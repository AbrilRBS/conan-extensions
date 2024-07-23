import json
import os
import textwrap
import requests

from rich.console import Console
from rich.table import Table
from rich.progress import Progress
from rich.text import Text

from conan.api.output import cli_out_write, ConanOutput
from conan.cli.args import common_graph_args, validate_common_graph_args
from conan.cli.printers.graph import print_graph_basic
from conan.cli.command import conan_command



def display_vulnerabilities(list_of_data_json):
    console = Console()
    console.print("\n")
    table = Table(title="Vulnerabilities", show_header=False, header_style="bold green")
    table.add_column("ID", style="dim", width=15)
    table.add_column("Details")

    total_vulnerabilities = 0
    packages_without_vulns = []
    if not list_of_data_json \
            or "errors" in list_of_data_json \
            or "data" not in list_of_data_json \
            or list_of_data_json["data"] is None:
        console.print("No vulnerabilities found")
        return

    for library_key, library_data in list_of_data_json["data"].items():
        vulnerabilities = library_data["vulnerabilities"]["edges"]
        ref = f"{library_key}/{library_data['version']}"
        if vulnerabilities:
            # Accumulate total vulnerabilities and add them to the table
            table.add_section()
            table.add_row(
                None,
                f"[red]{ref}: {len(vulnerabilities)} vulnerabilities[/]\n",
            )
            total_vulnerabilities += len(vulnerabilities)
            sorted_vulns = sorted(vulnerabilities, key=lambda x: x["node"]["name"])
            for vuln in sorted_vulns:
                node = vuln["node"]
                reference_url = node["references"][0] if node["references"] else "#"
                vulnerability_name = Text(node["name"], style="link " + reference_url)
                table.add_row(
                    vulnerability_name,
                    textwrap.shorten(node["description"], width=80, placeholder="...")
                )
        else:
            # Add package name to the list of packages without vulnerabilities
            packages_without_vulns.append(f"[white]{ref}[/]")

    # Print the table only if there are vulnerabilities found
    if total_vulnerabilities > 0:
        console.print(table)

    style = "bold yellow" if total_vulnerabilities > 0 else "bold green"
    console.print(
        f"Total vulnerabilities found: {total_vulnerabilities}", style=style
    )

    # Print the list of packages without vulnerabilities
    if packages_without_vulns:
        console.print(
            "No vulnerabilities found in: " + ", ".join(packages_without_vulns)
        )
    console.print("\n")
    console.print(
        "Vulnerability information provided by [link=https://jfrog.com/help/r/jfrog-catalog/jfrog-catalog]JFrog Catalog[/]",
        style="bold green")
    console.print("\n")


def json_formatter(results):
    cli_out_write(json.dumps(results, indent=4))


def get_cves_from_conan_proxy(refs, token):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    result = {"data": {}}

    console = Console(stderr=True)

    with Progress(console=console) as progress:
        task = progress.add_task("[cyan]Requesting information", total=len(refs))
        for ref in refs:
            progress.update(task, description=f"[cyan]Checking: {ref}", advance=0)
            response = requests.post(
                "https://conancenter-stg-api.jfrog.team/api/v1/query",
                headers=headers,
                json={
                    "reference": ref,
                },
            )
            if response.status_code == 200:
                result["data"].update(response.json()["data"])
            elif response.status_code == 429:
                msg = "[red]Rate limit exceeded. Results may be incomplete. "
                if not token:
                    msg += "Please [link=https://conancenter-stg.jfrog.team/private/register]register to get a token[/] to increase the rate limit."
                console.print(msg)
                break
            progress.update(task, description=f"[cyan]Checked: {ref}", advance=1)

    return result


def generate_graphql_custom_query(refs):
    return {}


def convert_custom_response(response):
    return {"data": {}}


def get_cves_from_jfrog_catalog(refs, token, catalog_url):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    query_json = generate_graphql_custom_query(refs)

    ConanOutput().info("[cyan]Requesting information...")
    response = requests.post(
        catalog_url,
        headers=headers,
        json=query_json,
    )
    ConanOutput().info("[green]Requested information...")
    result = convert_custom_response(response)
    return result


@conan_command(
    group="Security",
    formatters={"text": display_vulnerabilities, "json": json_formatter},
)
def catalog(conan_api, parser, *args):
    """
    Use JFrog Catalog to check security details for a Conan graph using a conanfile.txt/py.
    """
    common_graph_args(parser)
    parser.add_argument("--check-updates", default=False, action="store_true",
                        help="Check if there are recipe updates")
    parser.add_argument("--build-require", action='store_true', default=False,
                        help='Whether the provided reference is a build-require')

    parser.add_argument("--transitive", help="Load vulnerabilities for transitive dependencies", action="store_true", default=False)
    parser.add_argument("-t", "--token", help="Conan Catalog API token")
    parser.add_argument("--catalog-url", help="URL of the JFrog Catalog API. This option is only functional if you have your own JFrog Catalog instance.", default=None)
    args = parser.parse_args(*args)

    # parameter validation
    validate_common_graph_args(args)

    cwd = os.getcwd()
    path = conan_api.local.get_conanfile_path(args.path, cwd, py=None) if args.path else None

    # Basic collaborators, remotes, lockfile, profiles
    remotes = conan_api.remotes.list(args.remote) if not args.no_remote else []
    overrides = eval(args.lockfile_overrides) if args.lockfile_overrides else None
    lockfile = conan_api.lockfile.get_lockfile(lockfile=args.lockfile,
                                               conanfile_path=path,
                                               cwd=cwd,
                                               partial=args.lockfile_partial,
                                               overrides=overrides)
    profile_host, profile_build = conan_api.profiles.get_profiles_from_args(args)

    # TODO: Support pkglist?
    if not args.transitive and not path:
        # If --transitive is not passed, and no path, no need to expand any graph
        refs = args.requires
    else:
        if path:
            deps_graph = conan_api.graph.load_graph_consumer(path, args.name, args.version,
                                                             args.user, args.channel,
                                                             profile_host, profile_build, lockfile,
                                                             remotes, args.update,
                                                             check_updates=args.check_updates,
                                                             is_build_require=args.build_require)
        else:
            deps_graph = conan_api.graph.load_graph_requires(args.requires, args.tool_requires,
                                                             profile_host, profile_build, lockfile,
                                                             remotes, args.update,
                                                             check_updates=args.check_updates)
        print_graph_basic(deps_graph)
        if deps_graph.error:
            return {"error": deps_graph.error}

        if args.transitive:
            refs = list(set(f"{node.ref.name}/{node.ref.version}" for node in deps_graph.nodes[1:]))
        else:
            root_node = deps_graph.nodes[1]
            refs = list(set(f"{dep.ref.name}/{dep.ref.version}" for dep in root_node.dependencies.direct))

    ConanOutput().info(f"Requesting vulnerability information from the JFrog Catalog for: {', '.join(refs)}")

    if args.catalog_url is not None:
        return get_cves_from_jfrog_catalog(refs, args.token, args.catalog_url)
    else:
        return get_cves_from_conan_proxy(refs, args.token)
