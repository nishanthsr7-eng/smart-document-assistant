"""Schema fuzz over the read endpoints: whatever the OpenAPI document says, hold to it.

The value is in the cases nobody wrote: a path parameter that is empty, a unicode doc_id, a query
string with the wrong type. The check is narrow on purpose -- no 5xx, and the response matches the
schema the document advertises -- because those two are true of every endpoint by definition, so a
new route inherits the coverage without anyone adding a case for it.

Only safe methods are fuzzed. Handing a random-input generator the delete and erase endpoints
would test the fixtures' tolerance for data loss, not the API.
"""

import pytest
import schemathesis
from hypothesis import HealthCheck, settings
from schemathesis.checks import not_a_server_error
from schemathesis.specs.openapi.checks import response_schema_conformance

from src.api import deps, router
from src.auth.principal import Principal

ADMIN = Principal(
    user_id="00000000-0000-0000-0000-000000000001",
    tenant_id="00000000-0000-0000-0000-0000000000aa",
    email="admin@acme.test",
    role="admin",
)

schema = schemathesis.openapi.from_asgi("/openapi.json", router.app)


@pytest.fixture(autouse=True)
def authenticated():
    """Auth itself is covered in test_auth.py. Fuzzing it would only ever produce 401s."""
    router.app.dependency_overrides[deps.current_principal] = lambda: ADMIN
    yield
    router.app.dependency_overrides.clear()


# The job event stream is excluded: it is SSE, so it is a long poll with no response
# body to conform to, and tests/test_sse_contract.py is where its frames are checked.
@schema.include(method="GET").exclude(path_regex="/events$").parametrize()
@settings(max_examples=20, deadline=None, suppress_health_check=list(HealthCheck))
def test_read_endpoints_conform_to_their_schema(case, authenticated):
    response = case.call()
    # Status-code conformance is deliberately not among them. The 401, 403 and 404 these
    # endpoints raise come from HTTPException and are not in the generated document, so that
    # check would report the document's silence on every route rather than anything about this
    # request. Documenting those responses is production-readiness item 18's remaining half.
    case.validate_response(response, checks=(not_a_server_error, response_schema_conformance))
