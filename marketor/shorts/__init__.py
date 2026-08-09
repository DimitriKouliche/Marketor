"""
Automated shorts pipeline.

Turns long gameplay recordings plus the game's own telemetry log into finished
vertical clips, then publishes them. Stages are decoupled through a SQLite job
table so a failure at publish time never costs a re-render.

    ingest -> select -> render -> qc -> publish
"""

__all__ = ["config", "db", "telemetry", "ingest", "select", "render", "qc"]
