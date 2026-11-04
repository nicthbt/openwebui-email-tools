"""
title: Email tools
author: Nicolas THIBAUT
git_url: https://github.com/nicthbt/openwebui-email-tools
description: Search on mail server for information and fetch specific message content.
license: AGPL-3.0-only
version: 1.6.2
required_open_webui_version: 0.10.2
requirements: imapclient
"""

import asyncio
import json
import logging
import mimetypes
import os
import re
import time
from datetime import datetime
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, getaddresses, make_msgid
from html import escape, unescape

from fastapi import Request
from open_webui.models.files import Files
from open_webui.storage.provider import Storage
from pydantic import BaseModel, Field

from src.clients.mail_client import MailClient
from src.clients.terminal_client import OpenTerminalClient, OpenTerminalException
from src.common.decorators import custom_tool, with_context

log = logging.getLogger(__name__)


@custom_tool
class Tools:
    class UserValves(BaseModel):
        mailaddr: str = Field(
            title="Email address",
            default=None,
            pattern="^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]+$",
        )
        password: str = Field(
            title="Email password",
            default=None,
            json_schema_extra={"input": {"type": "password"}},
        )

    class Valves(BaseModel):
        protocol: str = Field(
            title="Protocol",
            default="imap",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": [
                        {"value": "imap", "label": "IMAP"},
                    ],
                }
            },
        )
        verify_ssl: bool = Field(
            title="SSL verification",
            default=True,
        )
        host: str = Field(
            title="Server hostname or IP address",
            default="host.docker.internal",
        )
        port: int | None = Field(
            title="Server port",
            default=None,
            ge=1,
            le=65535,
        )
        search_count: int = Field(
            title="Search result count",
            default=20,
        )
        search_timeout: int = Field(
            title="Search timeout",
            default=60,
        )

    def __init__(self):
        super().__init__("tools.email")

    def _get_credentials(self, config: dict) -> dict:
        if config.mailaddr is None:
            raise ValueError("Please configure email address")

        if config.password is None:
            raise ValueError("Please configure email password")

        return config.mailaddr.strip(), config.password.strip()

    def _get_handlers(self) -> tuple:
        # Return handlers
        match self.valves.protocol:
            case "imap":
                connect_handler = self._connect_imap
                browse_handler = self._browse_imap
                download_handler = self._download_imap
                draft_handler = self._draft_imap

            case _:
                raise ValueError("Unknown protocol")

        return (
            connect_handler,
            browse_handler,
            download_handler,
            draft_handler,
        )

    async def _connect_imap(
        self,
        __user__: dict,
        __event_call__: callable = None,
    ) -> MailClient:
        mailaddr, password = self._get_credentials(__user__.get("valves"))
        session = MailClient(
            host=self.valves.host,
            port=self.valves.port or 993,
            verify=self.valves.verify_ssl,
        )
        session.login(mailaddr, password)
        return session

    def _disconnect(self, session) -> None:
        if hasattr(session, "logout"):
            session.logout()

    def _browse_imap(
        self,
        session,
        query: str,
        unread: bool,
        mailboxes: list,
        timeout: int = None,
    ) -> list:
        results = []
        timeout = timeout or int(time.monotonic() + self.valves.search_timeout)

        # Extract search keywords
        keywords = self._extract_keywords(query)

        # Build search criteria
        criteria = self._build_criteria(keywords)

        # Look for unread messages only
        criteria = f"(UNSEEN {criteria})" if unread else criteria

        # List mailboxes
        if len(mailboxes) == 0:
            mailboxes = session.list(ignore=["TRASH", "BIN", "JUNK", "SPAM"])

        try:
            for mailbox in mailboxes:
                if int(time.monotonic()) >= timeout:
                    raise TimeoutError(
                        f"Timeout of search task after {self.valves.search_timeout} secs"
                    )

                # Search for messages
                uids = session.search(mailbox, criteria)

                for uid in uids:
                    # Fetch message metadata by UID
                    headers, attachments = session.fetch_metadata(mailbox, uid)

                    message_id = headers.get("Message-ID", None)
                    date = headers.get("Date", None)
                    subject = headers.get("Subject", None)
                    sender = next(iter(headers.get("From", [None])))
                    to = headers.get("To", [])
                    cc = headers.get("Cc", [])
                    recipients = [
                        formataddr((name, addr))
                        for name, addr in set(getaddresses(to + cc))
                    ]

                    if all([message_id, date, subject, sender]):
                        # Check that message ID has the correct format
                        if re.search(r"^(<.+>)$", message_id) is not None:
                            results.append(
                                self._score_message(
                                    mailbox,
                                    message_id,
                                    date,
                                    subject,
                                    sender,
                                    recipients,
                                    attachments,
                                    keywords,
                                )
                            )

        except TimeoutError as e:
            log.warning(e)

        # Sort results and return best matches
        return self._sort_results(results, [("score", True), ("date", True)])

    def _download_imap(self, session, mailbox: str, message_id: str) -> bytes:
        # Search for the message
        criteria = f"(HEADER Message-ID {json.dumps(message_id)})"
        uids = session.search(mailbox, criteria)

        if len(uids) == 0:
            raise ValueError(f"Unable to find message {message_id}")

        return session.fetch_raw(mailbox, next(iter(uids)))

    def _draft_imap(
        self,
        session,
        subject: str,
        sender: str,
        to: list,
        cc: list,
        attachments: dict,
        references: list,
        body_text: str,
        body_html: str,
    ) -> dict:
        # List draft mailboxes
        mailboxes = session.list(draft=True)

        if len(mailboxes) != 1:
            raise ValueError(f"Invalid number of draft mailboxes ({len(mailboxes)})")

        # Select draft mailbox
        mailbox = next(iter(mailboxes))

        # Generate message ID
        message_id = make_msgid(domain=sender.split("@")[-1])

        # Freeze date
        date = datetime.now().replace(microsecond=0).astimezone()

        # Create draft message
        msg = EmailMessage(policy=policy.SMTP)

        # Fill headers
        msg["Message-ID"] = message_id
        msg["Date"] = format_datetime(date)

        if subject:
            msg["Subject"] = subject
        if sender:
            msg["From"] = sender
        if to:
            msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        if references:
            msg["References"] = " ".join(references)
            msg["In-Reply-To"] = references[-1]

        # Set plain text body and alternative
        if body_text is not None:
            msg.set_content(body_text)
            if body_html is not None:
                msg.add_alternative(body_html, subtype="html")

        # Attach files
        for filename, content in attachments.items():
            mimetype, encoding = mimetypes.guess_type(filename)
            if mimetype is not None:
                maintype, subtype = mimetype.split("/")
            else:
                maintype, subtype = "application", "octet-stream"
            msg.add_attachment(
                content,
                maintype=maintype,
                subtype=subtype,
                filename=filename,
            )

        # Add message to mailbox
        session.append(mailbox, msg.as_bytes(), draft=True)

        # Merge recipients
        recipients = (to or []) + (cc or [])

        return self._format_result(
            mailbox,
            message_id,
            date,
            subject,
            sender,
            recipients,
            list(attachments.keys()),
        )

    def _build_history(self, msg: EmailMessage) -> tuple:
        part_text = msg.get_body(preferencelist=("plain",))
        part_html = msg.get_body(preferencelist=("html",))
        body_text = None
        body_html = None
        history_text = None
        history_html = None

        if part_text is not None:
            body_text = part_text.get_content().strip()
        if part_html is not None:
            body_html = part_html.get_content().strip()
            # Remove document wrappers
            body_html = re.sub(
                r"^.*?<body[^>]*>",
                "",
                body_html,
                flags=re.IGNORECASE | re.DOTALL,
            )
            body_html = re.sub(
                r"</body\s*>.*$",
                "",
                body_html,
                flags=re.IGNORECASE | re.DOTALL,
            )

        # Build plain text fallback from HTML
        if body_text is None and body_html is not None:
            # Replace HTML line break tag with plain text line break
            body_text = re.sub(r"<br\s*/?>", "\n", body_html, flags=re.IGNORECASE)
            # Replace specific HTML end tag with plain text line break
            body_text = re.sub(
                r"</(p|div|li|tr|h[1-6])\s*>",
                "\n",
                body_text,
                flags=re.IGNORECASE,
            )
            # Remove any other HTML tag
            body_text = re.sub(r"<[^>]+>", "", body_text)
            body_text = unescape(body_text).strip()

        # Build HTML fallback from plain text
        if body_html is None and body_text is not None:
            # Replace plain text line break with HTML line break tag
            body_html = "<br>".join(
                escape(line) for line in body_text.splitlines()
            ).strip()

        # Format reply history header
        sender = msg.get("From", None)
        date = msg.get("Date", None)

        if sender is not None:
            if date is not None:
                history_header = f"On {date}, {sender} wrote:"
            else:
                history_header = f"{sender} wrote:"
        else:
            history_header = "Original message:"

        # Quote reply history
        if body_text:
            history_text = "\n".join(
                f"> {line}" if line else ">" for line in body_text.splitlines()
            )
            history_text = f"{history_header}\n\n{history_text}"
        if body_html:
            history_html = (
                f'<div class="reply-attribution">{escape(history_header)}</div><br>'
                f'<blockquote type="cite">{body_html}</blockquote>'
            )

        return history_text, history_html

    def _get_reply_details(self, mailaddr: str, content: bytes) -> tuple:
        # Parse message content
        msg = message_from_bytes(content, policy=policy.default)

        # Format subject
        subject = msg.get("Subject", None)
        subject = (
            f"Re: {subject}"
            if subject is not None and not subject.upper().startswith("RE:")
            else subject
        )

        # Set sender as recipient
        to = msg.get_all("Reply-To", []) or msg.get_all("From", [])
        to = [
            formataddr((name, addr))
            for name, addr in set(getaddresses(to))
            if addr != mailaddr
        ]

        # Add extra recipients for carbon copy
        cc = msg.get_all("To", []) + msg.get_all("Cc", [])
        cc = [
            formataddr((name, addr))
            for name, addr in set(getaddresses(cc))
            if addr != mailaddr
        ]

        # Retrieve conversation
        references = msg.get("References", None) or msg.get("In-Reply-To", None)
        references = references.strip().split(" ") if references is not None else []

        # Retrieve message ID
        message_id = msg.get("Message-ID", None)

        # Append message ID to conversation
        if message_id is not None and message_id.strip() not in references:
            references.append(message_id.strip())

        # Build conversation history
        history_text, history_html = self._build_history(msg)

        return subject, to, cc, references, history_text, history_html

    def _score_message(
        self,
        mailbox: str,
        message_id: str,
        date: datetime,
        subject: str,
        sender: str,
        recipients: list,
        attachments: list,
        keywords: list,
    ) -> dict:
        # Initialize score to zero
        score = 0

        # Calculate the keywords total length
        total_length = sum(len(keyword) for keyword in keywords)

        # Calculate the weight of one character
        match_weight = 1.0 / max(1.0, total_length)

        if subject is not None:
            # Calculate match with subject
            score = score + sum(
                match_size * match_weight
                for match_size in self._seq_match(subject, keywords)
            )

        if sender is not None:
            # Calculate match with sender
            score = score + sum(
                match_size * match_weight
                for match_size in self._seq_match(sender, keywords)
            )

        for recipient in recipients:
            # Calculate match with recipient
            score = score + sum(
                match_size * match_weight
                for match_size in self._seq_match(recipient, keywords)
            )

        for attachment in attachments:
            mimetype, encoding = mimetypes.guess_type(attachment)

            # Lower weight for image, audio and video files
            match_weight = 0.5 if self._is_media(mimetype) else 1.0

            # Calculate the weight of one character
            match_weight = match_weight / max(1.0, total_length)

            # Calculate match with attachment
            score = score + sum(
                match_size * match_weight
                for match_size in self._seq_match(attachment, keywords)
            )

        return self._format_result(
            mailbox,
            message_id,
            date,
            subject,
            sender,
            recipients,
            attachments,
            score,
        )

    def _format_result(
        self,
        mailbox: str,
        message_id: str,
        date: datetime,
        subject: str,
        sender: str,
        recipients: list,
        attachments: list,
        score: float = None,
    ) -> dict:
        result = {
            "path": f"/{mailbox}/{message_id}/{subject.replace('/', '-')}.eml",
            "date": date.isoformat(),
            "mailbox": mailbox,
            "subject": subject,
            "sender": sender,
            "recipients": recipients,
            "attachments": attachments,
        }
        if score is not None:
            result.update({"score": score})
        return result

    def _build_criteria(self, keywords: list) -> str:
        if len(keywords) >= 1:
            keyword = f"(TEXT {json.dumps(keywords[0], ensure_ascii=False)})"
            if len(keywords) >= 2:
                return f"(OR {keyword} {self._build_criteria(keywords[1:])})"
            return keyword
        return "(ALL)"

    @with_context
    async def search_email_messages(
        self,
        query: str = None,
        unread: bool = False,
        mailboxes: list = [],
        __request__: Request = None,
        __user__: dict = None,
        __metadata__: dict = None,
        __event_emitter__: callable = None,
        __event_call__: callable = None,
    ) -> str:
        """
        Search for messages on mail server.
        Best for quickly identifying relevant messages from INBOX or other mailboxes.

        :param query: The search keywords to look up without special operators or wildcards (optional)
        :param unread: Flag to only look for new messages (optional)
        :param mailboxes: A list of mailboxes to look into (optional, defaults to all except "Trash", "Bin", "Junk" and "Spam")
        :return: JSON with results containing virtual path, date, mailbox, subject, sender, recipients, attachments and search score of each message
        """
        session, browse_handler, download_handler, draft_handler = self.context.get()

        await self._emit_status(
            __event_emitter__,
            "Searching for messages...",
            done=False,
        )

        # Browse messages on mail server
        results = await asyncio.to_thread(
            browse_handler, session, query, unread, mailboxes
        )

        await self._emit_status(
            __event_emitter__,
            f"{len(results)} messages found.",
            done=True,
        )

        return json.dumps(results, ensure_ascii=False)

    @with_context
    async def inspect_email_messages(
        self,
        query: str,
        messages: list,
        __request__: Request = None,
        __user__: dict = None,
        __metadata__: dict = None,
        __event_emitter__: callable = None,
        __event_call__: callable = None,
    ) -> str:
        """
        Search for information in specific messages on mail server.
        Best for semantic content retrieval.

        :param query: The search query to look up with the RAG engine
        :param messages: A list of path for messages to look into
        :return: JSON with results containing file ID, source path and search snippets for each message
        """
        session, browse_handler, download_handler, draft_handler = self.context.get()

        await self._emit_status(
            __event_emitter__,
            f"Inspecting {len(messages)} messages...",
            done=False,
        )

        collections = []

        for path in messages:
            # Extract mailbox, message ID and filename
            match = re.search(r"^/(.+)/(<[^>]+>)/(.*)$", path)
            mailbox, message_id, filename = (
                match.group(1),
                match.group(2),
                match.group(3),
            )
            mimetype, encoding = mimetypes.guess_type(filename)

            if not mimetype.startswith("message/"):
                raise TypeError(f"Invalid mimetype '{mimetype}' for '{path}'")

            log.info(f"Downloading '{path}'")
            content = await asyncio.to_thread(
                download_handler, session, mailbox, message_id
            )

            # Upload file and process content
            file_id, file_collection = await self._upload_file(
                path,
                filename,
                mimetype,
                content,
                process=True,
                __user__=__user__,
                __request__=__request__,
            )

            collections.append(file_collection)

        results = await self._query_collections(
            query,
            collections,
            __user__=__user__,
            __request__=__request__,
        )

        await self._emit_sources(
            __event_emitter__,
            results,
        )

        await self._emit_status(
            __event_emitter__,
            f"{len(results)} results found.",
            done=True,
        )

        return json.dumps(results, ensure_ascii=False)

    @with_context
    async def write_email_message(
        self,
        body_text: str,
        body_html: str,
        reply_to: str = None,
        subject: str = None,
        to: list = None,
        cc: list = None,
        attachments: list = None,
        __request__: Request = None,
        __user__: dict = None,
        __metadata__: dict = None,
        __event_emitter__: callable = None,
        __event_call__: callable = None,
    ) -> str:
        """
        Write a draft message on mail server.

        :param body_text: Plain text version of the body
        :param body_html: HTML version of the body
        :param reply_to: The path of the message to reply to (optional)
        :param subject: The subject of a new message (overwritten for a reply)
        :param to: A list of recipients for a new message (overwritten for a reply)
        :param cc: A list of extra recipients for carbon copy (optional, overwritten for a reply)
        :param attachments: A list of file ID or filesystem path (optional)
        :return: JSON with result containing virtual path, date, mailbox, subject, sender and recipients of the draft message
        """
        session, browse_handler, download_handler, draft_handler = self.context.get()

        await self._emit_status(
            __event_emitter__,
            "Drafting message...",
            done=False,
        )

        mailaddr, _ = self._get_credentials(__user__.get("valves"))

        references = []
        history_text = None
        history_html = None

        if reply_to is not None:
            path = reply_to

            # Extract mailbox, message ID and filename
            match = re.search(r"^/(.+)/(<[^>]+>)/(.*)$", path)
            mailbox, message_id, filename = (
                match.group(1),
                match.group(2),
                match.group(3),
            )
            mimetype, encoding = mimetypes.guess_type(filename)

            if mimetype is None or not mimetype.startswith("message/"):
                raise TypeError(f"Invalid mimetype '{mimetype}' for '{path}'")

            log.info(f"Downloading '{path}'")
            content = await asyncio.to_thread(
                self._download_imap, session, mailbox, message_id
            )

            # Extract details from message
            subject, to, cc, references, history_text, history_html = (
                self._get_reply_details(mailaddr, content)
            )

            # Merge history with body
            if body_text is not None and history_text is not None:
                body_text = f"{body_text}\n\n{history_text}"
            if body_html is not None and history_html is not None:
                body_html = f"{body_html}{history_html}"

        # Init Open Terminal client
        terminal = None
        try:
            terminal = OpenTerminalClient(__request__, __metadata__)
        except OpenTerminalException as e:
            log.warning(e)

        files = {}
        attachments = attachments or []
        for attachment in attachments:
            # Retrieve file content based on the file ID or the filesystem path
            if attachment.startswith("/"):
                if terminal is None:
                    raise ValueError(f"No terminal available to fetch '{attachment}'")
                filename = os.path.basename(attachment)
                content = await asyncio.to_thread(
                    terminal.download_file,
                    attachment,
                )
                files.update({filename: content})
            else:
                file = await Files.get_file_by_id(attachment)
                if file is None:
                    raise ValueError(f"File not found '{attachment}'")
                filename = file.filename
                path = await asyncio.to_thread(
                    Storage.get_file,
                    file.path,
                )
                with open(path, "rb") as f:
                    content = f.read()
                files.update({filename: content})

        result = await asyncio.to_thread(
            draft_handler,
            session,
            subject,
            mailaddr,
            to,
            cc,
            files,
            references,
            body_text,
            body_html,
        )

        await self._emit_status(
            __event_emitter__,
            "Message draft done.",
            done=True,
        )

        return json.dumps(result, ensure_ascii=False)
