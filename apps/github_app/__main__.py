"""Run the GitHub App webhook receiver with the standard-library WSGI server."""

from wsgiref.simple_server import make_server

from .service import GitHubAppWSGI, service_from_env


def main() -> None:
    app = GitHubAppWSGI(service_from_env())
    server = make_server("0.0.0.0", 8080, app)
    server.serve_forever()


if __name__ == "__main__":
    main()
