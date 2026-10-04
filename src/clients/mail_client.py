import logging
import ssl
from email.header import decode_header, make_header

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError, LoginError
from imapclient.response_types import BodyData, Envelope

from src.common.exceptions import CustomToolException

log = logging.getLogger(__name__)


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
