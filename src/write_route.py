"""A Starlette route for GET-shaped mutations that must not inherit HEAD.

Starlette automatically adds HEAD to every GET route. That is right for reads, but on
this service four GET routes mutate the store; running one as HEAD would hide the reply
while still appending a message, writing a note, or spending a signed nonce.
"""

from starlette.routing import Route


class WriteRoute(Route):
    """Keep GET routable while making HEAD fail method matching before the endpoint."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        assert self.methods is not None  # Route gives function endpoints GET (and HEAD)
        self.methods.discard("HEAD")
