import sys
from pathlib import Path

# Generated protobuf stubs use absolute imports (e.g. `from sf.substreams.v1 import ...`).
# Add this directory to sys.path so those imports resolve without installing as a package.
_proto_root = str(Path(__file__).parent)
if _proto_root not in sys.path:
    sys.path.insert(0, _proto_root)
