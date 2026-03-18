# ADS Browser + pCloud workflow

CLI script for ADS Browser API + pCloud.

## What the script does

1. Starts an existing ADS Browser profile by `profile_id`.
2. Connects to the started browser through CDP.
3. Reads the active pCloud session from the browser profile.
4. Creates a folder in the pCloud root.
5. Sends pCloud folder invites to emails from `email/emails.txt`.
6. Sends invites in batches of 20 emails until the file is exhausted.

The actual pCloud actions are executed through HTTP requests to the official
pCloud API. ADS Browser is only used to open the saved browser profile and
reuse its authenticated pCloud session.

## Project structure

```text
config.py             # ADS API and workflow settings
main.py               # interactive CLI
ads_api.py            # ADS Browser Local API client
pcloud_client.py      # pCloud HTTP API client
storage.py            # local SQLite storage for ADS profiles
workflow.py           # end-to-end pCloud workflow
edit/NAME.txt         # folder name
edit/text.txt         # message body for invites
email/emails.txt      # email list, one email per line
logs/app.log          # runtime log file
```

## Installation

Use Python 3.10+.

```bash
python3 -m pip install -r requirements.txt
```

## Configure ADS Browser API

Edit `config.py`:

```python
ADS_API_BASE = "http://127.0.0.1:50325"
ADS_API_KEY = "YOUR_ADS_API_KEY"
```

The API key is passed in the `Authorization: Bearer ...` header.

## Input files

### `edit/NAME.txt`

Folder name that will be created in the pCloud root.

### `edit/text.txt`

Message that will be sent together with folder invites.

### `email/emails.txt`

One email per line:

```text
first@example.com
second@example.com
third@example.com
```

## Run

```bash
python3 main.py
```

## Menu

1. Add ADS profile
2. Show saved profiles
3. Check pCloud session for a profile
4. Run folder creation + invite workflow
5. Delete profile
6. Exit

When adding a profile you can save:

- local profile title
- ADS Browser `profile_id`
- start URL
- parent pCloud folder id
- permission level (`view`, `edit`, `manage`, or raw integer)

## Permission mapping

The script uses pCloud API permission flags:

- `view` = `0`
- `edit` = `3` (`create + modify`)
- `manage` = `7` (`create + modify + delete`)

## Important notes

- Log in to pCloud inside the ADS Browser profile before running the workflow.
- If the session is missing, the script will ask you to log in manually and retry.
- Folder invites are sent through pCloud API one email at a time, but the script
  processes them in batches of 20 emails to match the requested workflow.
