import typer

app = typer.Typer(help="Pipeline incident agent", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Pipeline incident agent."""


@app.command()
def version() -> None:
    """Print the package version."""
    from importlib.metadata import version as v

    print(v("pipeline-incident-agent"))
