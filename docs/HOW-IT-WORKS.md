# How wa2ios works

## Pipeline

```
Android phone                       PC (wa2ios)                                     iPhone
─────────────                       ───────────                                     ──────
msgstore.db.crypt15 ──adb pull──▶  decrypt (AES-GCM, your 64-digit key)
WhatsApp/Media      ──adb pull──▶  msgstore.db  (SQLite)
                                       │ read chats + messages (android/reader.py)
                                       ▼
          iTunes/Apple Devices ──▶  unencrypted backup ──clone──▶ copy
                                       │ extract ChatStorage.sqlite (+ -wal)
                                       │ merge (ios/chatstorage.py)
                                       │ put back, fix Manifest.db, add media
                                       │ verify (verify.py)
                                       ▼
                                    patched copy ──Restore Backup──▶ WhatsApp with old chats
```

## Android side

- `msgstore.db.crypt15` is a protobuf header, then AES-256-GCM over a zlib-compressed SQLite
  file. The key is derived from the 64-digit root key ("backup encryption" HMAC loop). This
  is done by `wa-crypt-tools`.
- Modern schema only (WhatsApp 2021+): `message`, `chat`, `jid`, plus side tables
  `message_media`, `message_location`, `message_vcard`.
- `@lid` (hidden-number) addresses are mapped to phone numbers through `jid_map` where
  possible, so they land in the same iPhone chat as the contact's number. Self-chosen
  names come from `lid_display_name`.
- `timestamp` is milliseconds since 1970.

## iPhone side

- The backup index `Manifest.db` holds `Files(fileID, domain, relativePath, flags, file)`.
  `fileID = sha1(domain + "-" + relativePath)`, and the content lives at `<fileID[:2]>/<fileID>`.
  `file` is an NSKeyedArchiver `MBFile` plist whose `Size` must match the content.
- WhatsApp's store is `AppDomainGroup-group.net.whatsapp.WhatsApp.shared` / `ChatStorage.sqlite`,
  a Core Data database in WAL mode. Dates are seconds since 2001-01-01.
- Media files live at `Message/<ZMEDIALOCALPATH>` in that domain, with
  `ZMEDIALOCALPATH = Media/<chat-jid>/<x>/<y>/<uuid>.<ext>`.

### Writing rules

| Rule | Why |
|---|---|
| Only write columns that `PRAGMA table_info` reports | A different WhatsApp version gets rows in its own shape |
| New `Z_PK` values come from `Z_PRIMARYKEY.Z_MAX` (or from the highest existing row), and the counter is advanced | Otherwise WhatsApp would later reuse an id and overwrite a row |
| `ZFLAGS`, `ZMESSAGESTATUS`, `ZGROUPEVENTTYPE` and spotlight codes copy the commonest value of your own iPhone's text messages, per direction | They're undocumented. The app that will read the rows wrote those values |
| `ZSORT` is renumbered 1..N by date in each conversation that receives messages; `ZLASTMESSAGE`, `ZLASTMESSAGEDATE`, `ZLASTMESSAGETEXT`, `ZMESSAGECOUNTER` and `ZLASTSESSION` are updated | The chat list and the conversation order read from these |
| A message whose `ZSTANZAID` already exists in that conversation is skipped | No duplicates, and safe to run again |
| Incoming: `ZFROMJID` = the chat (the group's address in a group) and `ZGROUPMEMBER` = the sender; outgoing: `ZTOJID` = the chat | Matches what the app writes |
| After merging, the WAL is checkpointed into the main file, and the backup's `ChatStorage.sqlite-wal` is emptied | A stale log would be replayed over the new database on the phone |

### Message type mapping

| Android `message_type` | iPhone `ZMESSAGETYPE` |
|---|---|
| 0 text | 0 |
| 1 image | 1 (with `--with-media` and the file found), else a text line |
| 3 video | 2 |
| 13 GIF | 11 |
| 2 audio / voice (origin 1) | 3 |
| 9 document | 8 |
| 20 sticker | 15 |
| 4/14 contact, 5/16 location, 15/64 deleted, 66/106 poll, 42/43/82 view-once | 0, as a descriptive text line |
| 7 system, 10/11/90 calls, 99 album container | skipped |

## Safety checks before you restore

After writing, `verify.py` re-opens the copy independently and checks that:

- the Manifest is intact and has no leftover log,
- the file count equals the original's plus the added media,
- every WhatsApp file's recorded size matches its content,
- the store passes `integrity_check`,
- the message count equals the original's plus those added,
- every message has a conversation,
- `ZSORT` is unique within each conversation,
- the Core Data counters are ahead of every row.

If any check fails, the copy is deleted.
