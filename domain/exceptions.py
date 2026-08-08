class MDMSError(Exception):
    """Base domain exception for MDMS."""
    pass

class DatabaseError(MDMSError):
    """Raised when database operations fail."""
    pass

class IngestionError(MDMSError):
    """Raised when audio parsing or file hashing fails."""
    pass

class TaxonomyError(MDMSError):
    """Raised when taxonomy graph or alias resolution fails."""
    pass

class TaxonomyValidationError(TaxonomyError):
    """Raised when taxonomy ontology graph validation fails."""
    pass

class ResolverError(MDMSError):
    """Raised when symbolic inference resolution fails."""
    pass

class DiscoveryError(MDMSError):
    """Raised when recommendation or authority graph operations fail."""
    pass

class CircuitBreakerOpenError(MDMSError):
    """Raised when an external API endpoint circuit breaker is open."""
    pass

class InvalidStateError(MDMSError):
    """Raised on invalid entity state transitions."""
    pass