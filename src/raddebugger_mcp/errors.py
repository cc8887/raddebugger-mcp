class RadError(RuntimeError):
    """An actionable debugger or protocol error, reported as an MCP tool error."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")
