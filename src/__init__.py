"""AI Ops Agent for the insurance-api stack.

Tails Docker container logs, application logs, and Nginx access/error logs,
detects anomalies, asks an LLM for a root-cause analysis and suggested fix,
and optionally applies a small, allowlisted set of remediation actions.
"""

__version__ = "0.1.0"
