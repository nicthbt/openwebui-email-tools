# Open WebUI Email tools

Search on mail server for information and fetch specific message content using Open WebUI's RAG engine.

## Features

- Writes new messages with plain text or HTML content.
- Replies to conversations and threads.
- Searches for messages in specified mailboxes.
- Ranks search results using keywords scoring and email date.
- Downloads messages as EML files and processes content in vector database.
- Caches messages by content hash.
- Inspects messages and retrieves relevant parts.
- Secures identity and access management with isolated/user-based authentication.
- Supports IMAP protocol.

## Available tools

### `search_email_messages`

Searches for messages on the mail server and returns metadata.

Input parameters:

| Parameter | Description |
|---|---|
| `query` | Search query (optional). |
| `unread` | Filter on new messages only (optional). |
| `mailboxes` | Mailboxes to search (optional, defaults to all except Trash, Bin, Junk and Spam). |

The output contains for each result:

- Virtual path
- Date
- Mailbox
- Subject
- Sender
- Recipients
- Attachments
- Search score

### `inspect_email_messages`

Retrieves specific messages from the mail server and uses Open WebUI's retrieval engine to find relevant parts.

Input parameters:

| Parameter | Description |
|---|---|
| `query` | Search query. |
| `messages` | List of messages. |

The output contains for each result:

- Open WebUI file ID
- Source path
- Text snippets

### `write_email_message`

Creates a draft email by composing a new message or by replying to an existing message.

Input parameters:

| Parameter | Description |
|---|---|
| `body_text` | Plain-text version of the message body. |
| `body_html` | HTML version of the message body. |
| `reply_to` | Path of the message to reply to (optional). |
| `subject` | Subject of a new message. |
| `to` | List of recipients for a new message. |
| `cc` | List of carbon-copy recipients (optional). |

The output contains:

- Virtual path
- Date
- Mailbox
- Subject
- Sender
- Recipients
- Attachments

## Installation

1. Go to `Workspace` in Open WebUI.
2. Create a new tool from the `Tools` tab.
3. Paste the content of `openwebui_email_tools.py` and save the tool.
4. Enable the tool in your custom model in `Models`.
5. Configure the tool valves to change default settings.
6. Configure the mail address and password for each user.

## Configuration

### User Valves

| Setting | Description |
|---|---|
| `mailaddr` | Email account address. |
| `password` | Email account password. |

### Tool Valves

| Setting | Default | Description |
|---|---:|---|
| `protocol` | `imap` | Connection method. |
| `verify_ssl` | `true` | SSL certificates verification. |
| `host` | `host.docker.internal` | Server hostname or IP address reachable from the Open WebUI container. |
| `port` | Protocol default | Optional custom server port. |
| `search_count` | `20` | Maximum number of search results to return. |
| `search_timeout` | `60` | Maximum search task duration in seconds. |

When `port` is not set, the protocol default port (`993`) is used.

## Security

**Apple and Google require to setup an app password** to access your account over the IMAP protocol:
- Apple: https://account.apple.com/account/manage
- Google: https://myaccount.google.com/apppasswords

**Enable encryption** to securely store credentials:
- Set `WEBUI_SECRET_KEY` (generate a secure key with `openssl rand -hex 32`).
- Set `ENABLE_VALVE_ENCRYPTION` to `true`.

Restrict network access between Open WebUI and the calendar server.

## Compatibility

Tested with **Open WebUI 0.10.2**.

The tool imports internal Open WebUI modules, so compatibility with earlier or later releases is not guaranteed.

## Requirements

Allow Open WebUI to install listed requirements:
- Set `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS` to `true`.
- Set `OFFLINE_MODE` to `false`.

The tool relies on a 3rd party Python package:
- [imapclient](https://github.com/mjs/imapclient)

## License

[GNU AGPLv3](LICENSE)
