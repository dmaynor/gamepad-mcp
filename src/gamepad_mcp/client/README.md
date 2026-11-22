# gamepad_mcp.client — first-party remote-play clients

Implements the interface in `base.py` (`RemotePlayClient`): wake,
connect, grab_frame, send_state (→ InputAck), disconnect. Everything
above this layer — MCP server, driver, the PRD §4.10 panel, the
§4.11 physical-pad source — is a thin client of that surface plus
`sources.py` (InputSource/StatePump) and `log.py` (MoveLogger,
MOVELOG-format records).

Modules:
- `errors.py` — typed failures only; silence is a bug (PRD §4.4).
- `state.py` — ControllerState / InputAck / FrameResult / InputLogRecord.
- `stub.py` — in-memory client with failure injection, for tests.
- `ps.py` — PlayStation over chiaki-py (libchiaki). M1: connect +
  frames proven live; input is a typed refusal until M2.
- Xbox arrives as a sibling module implementing the same interface.

Registration material and tokens are credentials: read on the host,
held in memory, never logged.
