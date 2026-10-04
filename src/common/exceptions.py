class CustomToolException(Exception):
    """Specific exception that can be caught by the with_context decorator"""

    def __init__(self, message, error=None):
        super().__init__(message)
        self.error = error
