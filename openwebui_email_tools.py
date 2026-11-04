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


import logging
import ssl
from email.header import decode_header
from email.header import make_header
from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError
from imapclient.exceptions import LoginError
from imapclient.response_types import BodyData
from imapclient.response_types import Envelope
import requests
import io
import mimetypes
import os
import re
import unicodedata
from contextvars import ContextVar
from difflib import SequenceMatcher
from hashlib import blake2b
from fastapi import Request
from fastapi import UploadFile
from open_webui.internal.db import get_async_db_context
from open_webui.models.config import Config
from open_webui.models.files import Files
from open_webui.models.users import UserModel
from open_webui.retrieval.vector.async_client import ASYNC_VECTOR_DB_CLIENT
from open_webui.routers.files import upload_file_handler
from open_webui.routers.retrieval import ProcessFileForm
from open_webui.routers.retrieval import QueryCollectionsForm
from open_webui.routers.retrieval import process_file
from open_webui.routers.retrieval import query_collection_handler
import json
from functools import wraps
import asyncio
import time
from datetime import datetime
from email import message_from_bytes
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime
from email.utils import formataddr
from email.utils import getaddresses
from email.utils import make_msgid
from html import escape
from html import unescape
from open_webui.storage.provider import Storage
from pydantic import BaseModel
from pydantic import Field


log = logging.getLogger(__name__)


class CustomToolException(Exception):
    """Specific exception that can be caught by the with_context decorator"""

    def __init__(self, message, error=None):
        super().__init__(message)
        self.error = error


class MailException(CustomToolException):
    def __init__(self, message, error=None):
        super().__init__(message)
        self.error = error


class MailClient:
    def __init__(
        self,
        host: str,
        port: int,
        verify: bool = True,
    ):
        ssl_context = ssl.create_default_context()
        if not verify:
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        self.imap = IMAPClient(
            host=host,
            port=port,
            use_uid=True,
            ssl=True,
            ssl_context=ssl_context,
            timeout=10,
        )
        self.mailboxes = None
        self.selection = None
        self.readonly = None

    def login(self, mailaddr: str, password: str) -> None:
        try:
            self.imap.login(mailaddr, password)
        except LoginError as e:
            raise MailException("Invalid username or password", e)
        except IMAPClientError as e:
            raise MailException("Unable to login", e)

    def logout(self) -> None:
        try:
            self.imap.logout()
        except IMAPClientError as e:
            raise MailException("Unable to logout", e)

    def list(self, ignore: list = [], draft: bool = False) -> list:
        try:
            data = self.imap.list_folders()
            if not isinstance(data, list):
                raise MailException("Unable to parse response data", data)
        except IMAPClientError as e:
            raise MailException("Unable to list mailboxes", e)

        self.mailboxes = {}

        for flags, delimiter, mailbox in data:
            # Check flags if looking for draft mailbox
            if not draft or rb"\Drafts" in flags:
                # Check mailbox name against list to ignore
                if mailbox.upper() not in ignore:
                    self.mailboxes.update({mailbox: flags})

        return list(self.mailboxes)

    def select(self, mailbox: str, readonly: bool = True) -> None:
        mailboxes = self.list() if self.mailboxes is None else list(self.mailboxes)
        if mailbox not in mailboxes:
            raise MailException(f"Unable to find '{mailbox}' among {mailboxes}")

        try:
            self.imap.select_folder(mailbox, readonly=readonly)
        except IMAPClientError as e:
            raise MailException(f"Unable to select mailbox '{mailbox}'", e)

        self.selection = mailbox
        self.readonly = readonly

    def append(self, mailbox: str, content: bytes, draft: bool = False) -> None:
        if mailbox != self.selection or self.readonly:
            self.select(mailbox, readonly=False)

        try:
            self.imap.append(mailbox, content, flags=[rb"\Draft"] if draft else [])
        except IMAPClientError as e:
            raise MailException(f"Unable to draft message in '{mailbox}'", e)

    def search(self, mailbox: str, criteria: str) -> list:
        if mailbox != self.selection:
            self.select(mailbox)

        try:
            # Rely on imaplib rather than imapclient search function to send raw criteria
            # data = self.imap.search(criteria, charset="UTF-8")
            response, data = self.imap._imap.uid(
                "SEARCH", "CHARSET", "UTF-8", criteria.encode()
            )
            if response != "OK":
                raise MailException("Unable to parse response data", data)
            data = (
                list(map(int, data[0].decode().strip().split(" ")))
                if len(data[0])
                else []
            )
            if not isinstance(data, list):
                raise MailException("Unable to parse response data", data)
        except IMAPClientError as e:
            raise MailException(f"Unable to search mailbox '{mailbox}'", e)

        return data

    def fetch_raw(
        self,
        mailbox: str,
        uid: int,
    ) -> bytes:
        if mailbox != self.selection:
            self.select(mailbox)

        try:
            data = self.imap.fetch([uid], ["BODY.PEEK[]"])
            if not isinstance(data, dict):
                raise MailException("Unable to parse response data", data)
            data = data.get(uid, None)
            if not isinstance(data, dict):
                raise MailException("Unable to parse response data", data)
        except IMAPClientError as e:
            raise MailException(f"Unable to fetch message {uid} from '{mailbox}'", e)

        return data.get(b"BODY[]", None)

    def fetch_metadata(
        self,
        mailbox: str,
        uid: int,
    ) -> tuple:
        if mailbox != self.selection:
            self.select(mailbox)

        try:
            data = self.imap.fetch([uid], ["ENVELOPE", "BODYSTRUCTURE"])
            if not isinstance(data, dict):
                raise MailException("Unable to parse response data", data)
            data = data.get(uid, None)
            if not isinstance(data, dict):
                raise MailException("Unable to parse response data", data)
        except IMAPClientError as e:
            raise MailException(f"Unable to fetch message {uid} from '{mailbox}'", e)

        envelope = data.get(b"ENVELOPE", None)
        bodystructure = data.get(b"BODYSTRUCTURE", None)

        return self.get_headers(envelope), self.get_attachments(bodystructure)

    def get_headers(self, envelope: Envelope) -> dict:
        headers = {}
        if envelope.message_id is not None:
            headers.update(
                {
                    "Message-ID": envelope.message_id.decode().strip(),
                }
            )
        if envelope.date is not None:
            headers.update(
                {
                    "Date": envelope.date.astimezone(),
                }
            )
        if envelope.subject is not None:
            headers.update(
                {
                    "Subject": str(
                        make_header(decode_header(envelope.subject.decode().strip()))
                    ),
                }
            )
        if envelope.from_ is not None:
            headers.update(
                {
                    "From": [
                        str(make_header(decode_header(address)))
                        for address in map(str, envelope.from_)
                    ],
                }
            )
        if envelope.to is not None:
            headers.update(
                {
                    "To": [
                        str(make_header(decode_header(address)))
                        for address in map(str, envelope.to)
                    ],
                }
            )
        if envelope.cc is not None:
            headers.update(
                {
                    "Cc": [
                        str(make_header(decode_header(address)))
                        for address in map(str, envelope.cc)
                    ],
                }
            )
        return headers

    def get_attachments(self, part: BodyData) -> list:
        attachments = []
        if part.is_multipart and isinstance(part[0], list):
            for subpart in part[0]:
                attachments.extend(self.get_attachments(subpart))
        else:
            for element in part:
                if isinstance(element, tuple) and isinstance(element[0], bytes):
                    if element[0].decode().lower() in [
                        "attachment",
                        "inline",
                    ]:
                        if isinstance(element[1], tuple) and len(element[1]) % 2 == 0:
                            for key, value in zip(element[1][0::2], element[1][1::2]):
                                if key.decode().lower() == "filename":
                                    attachments.append(
                                        str(make_header(decode_header(value.decode())))
                                    )
        return attachments


class OpenTerminalException(CustomToolException):
    pass


class OpenTerminalClient:
    def __init__(self, __request__, __metadata__):
        self.http = requests.Session()
        self.http.cookies.update(__request__.cookies)

        # Forward auth header
        authorization = __request__.headers.get("Authorization", None)
        if authorization is not None:
            self.http.headers.update({"Authorization": authorization})

        # Forward chat ID
        chat_id = __metadata__.get("chat_id", None)
        if chat_id is not None:
            self.http.headers.update({"X-Session-Id": chat_id})

        # Extract terminal ID
        terminal_id = __metadata__.get("terminal_id", None)
        if terminal_id is None:
            raise OpenTerminalException("No terminal set for this chat")

        # Extract internal server
        host, port = __request__.scope.get("server", None) or (None, None)
        if host is None or port is None:
            raise OpenTerminalException("Unable to detect internal server")

        # Build API routes
        self.server = f"http://{host}:{port}"
        self.api = {
            "CWD": __request__.app.url_path_for(
                "proxy_terminal",
                server_id=terminal_id,
                path="files/cwd",
            ).make_absolute_url(base_url=self.server),
            "UPLOAD": __request__.app.url_path_for(
                "proxy_terminal",
                server_id=terminal_id,
                path="files/upload",
            ).make_absolute_url(base_url=self.server),
            "VIEW": __request__.app.url_path_for(
                "proxy_terminal",
                server_id=terminal_id,
                path="files/view",
            ).make_absolute_url(base_url=self.server),
        }

    def __del__(self):
        self.http.close()

    def get_cwd(self, timeout: int = 10) -> str:
        response = self.http.get(
            self.api.get("CWD"),
            timeout=timeout,
        )
        response.raise_for_status()

        # Load response as JSON
        response_json = response.json()

        return response_json.get("cwd", None)

    def upload_file(
        self,
        filename: str,
        mimetype: str,
        content: bytes,
        directory: str = None,
        timeout: int = 60,
    ) -> dict:
        response = self.http.post(
            self.api.get("UPLOAD"),
            params={"directory": directory or self.get_cwd()},
            files={"file": (filename, content, mimetype or "application/octet-stream")},
            timeout=timeout,
        )
        response.raise_for_status()

        # Load response as JSON
        response_json = response.json()

        path = response_json.get("path", None)
        size = response_json.get("size", None)

        return {
            "path": path,
            "name": filename,
            "size": size,
            "content_type": mimetype,
        }

    def download_file(self, path: str, timeout: int = 60) -> bytes:
        response = self.http.get(
            self.api.get("VIEW"),
            params={"path": path},
            stream=True,
            timeout=timeout,
        )
        response.raise_for_status()

        content = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            content.extend(chunk)
        return bytes(content)


class CustomTool:
    def __init__(self, namespace):
        self.valves = self.Valves()
        self.context = ContextVar(namespace)
        self.namespace = namespace
        # Add missing mimetypes
        msoffice = {
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        }
        libreoffice = {
            ".odt": "application/vnd.oasis.opendocument.text",
            ".ods": "application/vnd.oasis.opendocument.spreadsheet",
            ".odp": "application/vnd.oasis.opendocument.presentation",
        }
        for extension, mimetype in msoffice.items():
            mimetypes.add_type(mimetype, extension)
        for extension, mimetype in libreoffice.items():
            mimetypes.add_type(mimetype, extension)

    def _is_media(
        self,
        mimetype: str,
        checklist: list = ["image/", "audio/", "video/"],
    ) -> bool:
        if mimetype is not None:
            return mimetype.startswith(tuple(checklist))
        return False

    def _seq_match(self, text: str, keywords: list) -> list:
        # Normalize text in ascii characters
        nfkd_text = (
            unicodedata.normalize("NFKD", text.lower())
            .encode("ascii", "ignore")
            .decode()
        )
        # Normalize keywords in ascii characters
        nfkd_keywords = [
            unicodedata.normalize("NFKD", keyword.lower())
            .encode("ascii", "ignore")
            .decode()
            for keyword in keywords
        ]

        # Get the match length for each keyword
        return [
            SequenceMatcher(None, nfkd_keyword, nfkd_text).find_longest_match().size
            for nfkd_keyword in nfkd_keywords
        ]

    def _sort_results(self, results: list, keys: list) -> list:
        # Sort by keys from lowest to highest priority
        for key, reverse in reversed(keys):
            results.sort(key=lambda result: result[key], reverse=reverse)
        return results[: self.valves.search_count]

    def _extract_keywords(self, query: str) -> list:
        if query is None or len(query.strip()) == 0:
            return []

        # Split query on special characters (space, tab, comma, etc) and remove linking words
        keywords = set(
            keyword.strip()
            for keyword in re.split(r"[';,\s\t\r\n]+", query)
            if len(keyword.strip()) > 1
        )

        # Check that keywords are not empty
        if len(keywords) == 0:
            raise ValueError(f"Cannot build keywords from query string '{query}'")

        return list(keywords)

    async def _query_collections(
        self,
        query: str,
        collections: list,
        __user__: dict,
        __request__: Request,
    ) -> list:
        # Query the collection using the retrieval engine
        collection_results = await query_collection_handler(
            __request__,
            QueryCollectionsForm(
                collection_names=collections,
                query=query,
            ),
            user=UserModel(**__user__),
        )

        results = {}

        # Generate query-focused results (instead of relying on raw results)
        for distances, metadatas, documents in zip(
            collection_results.get("distances", []),
            collection_results.get("metadatas", []),
            collection_results.get("documents", []),
        ):
            for distance, metadata, document in zip(distances, metadatas, documents):
                file_id = metadata.get("file_id")
                file_metadata = await Files.get_file_metadata_by_id(file_id)
                source = file_metadata.meta.get("source") or metadata.get("source")
                source_hash = blake2b(source.encode()).hexdigest()
                # Add new source to results or update existing source with new snippets
                snippets = results.get(source_hash, {}).get("snippets", [])
                snippets.append(document)
                # Add new source to results or update existing source with new snippets
                results.update(
                    {
                        source_hash: {
                            "id": file_id,
                            "source": source,
                            "snippets": snippets,
                        }
                    }
                )

        return list(results.values())

    async def _get_cache_file(
        self,
        file_hash: str,
        user: UserModel,
    ) -> tuple:
        cache_key = f"{self.namespace}.files.{user.id}.{file_hash}"
        cache_value = await Config.get(cache_key, {})

        file_id = cache_value.get("id", None)
        file_collection = cache_value.get("collection", None)

        cleanup = False

        if file_id is not None:
            file = await Files.get_file_by_id(file_id)
            if not file:
                cleanup = True

        if file_collection is not None:
            collection = await ASYNC_VECTOR_DB_CLIENT.has_collection(file_collection)
            if not collection:
                cleanup = True

        # Delete cache if file or collection no longer exist
        if cleanup:
            log.warning(f"Deleting cache for {cache_key}")
            await Config.delete(cache_key)
            return None, None

        return file_id, file_collection

    async def _set_cache_file(
        self,
        file_hash: str,
        file_id: str,
        file_collection: str,
        user: UserModel,
    ) -> None:
        cache_key = f"{self.namespace}.files.{user.id}.{file_hash}"
        cache_value = {
            "id": file_id,
            "collection": file_collection,
        }
        await Config.upsert({cache_key: cache_value})

    async def _upload_file(
        self,
        source: str,
        filename: str,
        mimetype: str,
        content: bytes,
        process: bool,
        __user__: dict,
        __request__: Request,
    ) -> tuple:
        async with get_async_db_context() as db:
            user = UserModel(**__user__)

            # Search for file in cache
            file_hash = blake2b(source.encode() + b"\0" + content).hexdigest()
            file_id, file_collection = await self._get_cache_file(
                file_hash,
                user=user,
            )

            # Upload file if not in cache
            if file_id is None:
                log.info(f"Uploading '{filename}'")
                file = await upload_file_handler(
                    __request__,
                    UploadFile(
                        file=io.BytesIO(content),
                        filename=filename,
                        headers={"content-type": mimetype},
                    ),
                    metadata={},
                    process=False,
                    user=user,
                    db=db,
                )
                file_id = file.id

            # Update source
            await Files.update_file_metadata_by_id(
                file_id,
                {"source": source},
                db=db,
            )

            # Process file if not in cache
            if file_collection is None and process is True:
                log.info(f"Processing '{filename}'")
                result = await process_file(
                    __request__,
                    ProcessFileForm(file_id=file_id),
                    user=user,
                    db=db,
                )
                file_collection = result.get("collection_name")

            await self._set_cache_file(
                file_hash,
                file_id,
                file_collection,
                user=user,
            )

            return file_id, file_collection

    async def _emit_sources(
        self,
        event_emitter,
        files: list,
    ) -> None:
        for file in files:
            file_id = file.get("id")
            source = file.get("source")
            filename = os.path.basename(source)
            snippets = file.get("snippets")
            if event_emitter:
                await event_emitter(
                    {
                        "type": "source",
                        "data": {
                            "source": {
                                "id": file_id,
                                "name": filename,
                                "type": "file",
                            },
                            "document": snippets,
                            "metadata": [
                                {
                                    "file_id": file_id,
                                    "name": filename,
                                    "source": source,
                                }
                                for snippet in snippets
                            ],
                        },
                    }
                )

    async def _emit_files(
        self,
        event_emitter,
        files: list,
    ) -> None:
        if event_emitter:
            await event_emitter(
                {
                    "type": "files",
                    "data": {
                        "files": [
                            (
                                {**file, "type": "filesystem"}
                                if file.get("id", None) is None
                                else {**file, "type": "file"}
                            )
                            for file in files
                        ],
                    },
                }
            )

    async def _emit_status(
        self,
        event_emitter,
        desc: str,
        done: bool = False,
        hidden: bool = False,
    ) -> None:
        if event_emitter:
            await event_emitter(
                {
                    "type": "status",
                    "data": {
                        "description": desc,
                        "done": done,
                        "hidden": hidden,
                    },
                }
            )


def custom_tool(cls):
    return type(cls.__name__, (cls, CustomTool), {})


def with_context(func):
    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        session = None
        token = None

        try:
            __request__ = kwargs.get("__request__", None)
            __user__ = kwargs.get("__user__", None)
            __metadata__ = kwargs.get("__metadata__", None)
            __event_emitter__ = kwargs.get("__event_emitter__", None)
            __event_call__ = kwargs.get("__event_call__", None)

            if __request__ is None:
                raise ValueError("Request context not available")
            if __user__ is None:
                raise ValueError("User context not available")
            if __metadata__ is None:
                raise ValueError("Metadata context not available")
            else:
                if __metadata__.get("files", None) is None:
                    __metadata__["files"] = []

            connect_handler, *other_handlers = self._get_handlers()

            await self._emit_status(
                __event_emitter__,
                "Connecting to server...",
                done=False,
            )

            # Connect to server
            session = await connect_handler(__user__, __event_call__)

            # Set context for this call
            token = self.context.set((session, *other_handlers))

            return await func(self, *args, **kwargs)

        except CustomToolException as e:
            log.error(f"{e} ({e.error})" if e.error else str(e))
            return json.dumps({"error": str(e)})

        except Exception as e:
            log.exception(e)
            return json.dumps({"error": str(e)})

        finally:
            # Reset context for this call
            if token is not None:
                self.context.reset(token)

            # Disconnect from server
            if session is not None:
                self._disconnect(session)

    return wrapper


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
