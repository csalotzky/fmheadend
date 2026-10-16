# Local bench support

This is a self-contained snapshot of the existing wire codec, Linux client, RDS
encoder, error types and TxConfig fields, used only by firmware bench tools.
No host source checkout or uploader is imported.

Only package imports were adjusted in protocol.py, linux_client.py, encoder.py
and errors.py. client.py retains the TxConfig fields and original Pydantic model
settings. Daemon service interfaces, mock clients and persistent model conversion
are not bench dependencies and are not included. Do not change protocol, retry
policy or RDS encoding as part of path maintenance.
