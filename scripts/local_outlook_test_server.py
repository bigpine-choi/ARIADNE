"""Browser fixture: synthetic COM provider, production local service and DB."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))
from test_local_outlook import FakeLocal
from ariadne.demo import seed_demo
from ariadne.outlook.local import LocalOutlookService
from ariadne.repository import Repository
from ariadne.server import create_server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', type=Path, required=True)
    args = parser.parse_args()
    with Repository(args.db) as repo:
        seed_demo(repo)
        adapter = FakeLocal()
        adapter.messages[0]['subject'] = '<img src=x onerror="window.localXss=1"> 가상 업무 메일'
        adapter.failed = 1
        adapter.truncated = True
        local = LocalOutlookService(repo, adapter)
        server = create_server(repo, port=0, local_outlook=local)
        print(json.dumps({'url':f'http://127.0.0.1:{server.server_address[1]}'}), flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()


if __name__ == '__main__':
    main()
