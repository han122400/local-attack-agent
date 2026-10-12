"""Mock the streaming HTTP boundary while exercising real model parsing."""
from contextlib import contextmanager
from unittest.mock import Mock, patch

import httpx2 as httpx


@contextmanager
def mock_model(return_value=None, side_effect=None):
    opened = Mock(return_value=return_value, side_effect=side_effect)
    client_type = httpx.AsyncClient
    def respond(request):
        stream = opened(request)
        return httpx.Response(200, content=stream.getvalue())
    with patch("explorer.httpx.AsyncClient", side_effect=lambda **kwargs:
               client_type(transport=httpx.MockTransport(respond), **kwargs)):
        yield opened
