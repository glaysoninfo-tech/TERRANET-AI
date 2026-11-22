"""CLI administrativa do TERRANET CORE para bootstrap de clientes, instalações, usuários e EDGEs."""

from __future__ import annotations

import argparse
import json
import os

from core.storage.repository import CoreStore


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="terranet-core-admin")
    parser.add_argument(
        "--db-url",
        default=os.environ.get("TERRANET_CORE_DB_URL", "sqlite:///data/core.db"),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_tenant = sub.add_parser("create-tenant")
    p_tenant.add_argument("tenant_id")
    p_tenant.add_argument("name")

    p_site = sub.add_parser("create-site")
    p_site.add_argument("site_id")
    p_site.add_argument("tenant_id")
    p_site.add_argument("name")

    p_user = sub.add_parser("create-user")
    p_user.add_argument("username")
    p_user.add_argument("tenant_id")
    p_user.add_argument("--role", default="operator", choices=["admin", "operator", "viewer"])

    p_edge = sub.add_parser("register-edge")
    p_edge.add_argument("edge_id")
    p_edge.add_argument("tenant_id")
    p_edge.add_argument("site_ids", nargs="+")

    args = parser.parse_args(argv)
    store = CoreStore.open(args.db_url)
    try:
        if args.cmd == "create-tenant":
            print(json.dumps(store.create_tenant(args.tenant_id, args.name), indent=2))
        elif args.cmd == "create-site":
            print(json.dumps(store.create_site(args.site_id, args.tenant_id, args.name), indent=2))
        elif args.cmd == "create-user":
            user, api_key = store.create_user(args.username, args.tenant_id, role=args.role)
            print(json.dumps({"user": user, "api_key": api_key}, indent=2))
        elif args.cmd == "register-edge":
            edge, token = store.register_edge(args.edge_id, args.tenant_id, args.site_ids)
            print(json.dumps({"edge": edge, "token": token}, indent=2))
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
