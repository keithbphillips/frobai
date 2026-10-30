"""Command-line entry point: frobai STORY [options]"""

import argparse
import os
import sys
import threading

from .ai.llm import LLMError, OllamaClient
from .ai.parser import AIParser
from .session import Session
from .ui import TerminalHost, TerminalUI
from .zmachine import ZMachine, ZMachineError


def main(argv=None):
    ap = argparse.ArgumentParser(prog="frobai", description="Z-machine interpreter with an AI-assisted parser.")
    ap.add_argument("story", help="Z-code story file (.z1-.z5, .z7, .z8, .dat)")
    ap.add_argument("--model", default=os.environ.get("FROBAI_MODEL"),
                    help="Ollama model (default: $FROBAI_MODEL, else the smallest installed model)")
    ap.add_argument("--ollama-url", default=os.environ.get("OLLAMA_HOST", "http://localhost:11434"))
    ap.add_argument("--no-ai", action="store_true", help="plain interpreter, no AI parser")
    ap.add_argument("--parser", choices=["llm", "laya"], default=os.environ.get("FROBAI_PARSER", "llm"),
                    help="rewrite commands with the Ollama model (llm) or with Laya (laya); "
                         "questions to the companion always use the Ollama model")
    ap.add_argument("--quiet-ai", action="store_true", help="don't show the commands the AI substitutes")
    ap.add_argument("--debug-ai", action="store_true", help="print AI prompts and responses")
    ap.add_argument("--width", type=int, help="screen width in characters")
    ap.add_argument("--seed", type=int, help="random seed (for reproducible play)")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--no-status", action="store_true", help="don't display the status line / upper window")
    args = ap.parse_args(argv)

    if args.ollama_url and not args.ollama_url.startswith("http"):
        args.ollama_url = "http://" + args.ollama_url

    try:
        with open(args.story, "rb") as f:
            story = f.read()
    except OSError as e:
        ap.error(str(e))

    ui = TerminalUI(width=args.width, color=False if args.no_color else None, show_status=not args.no_status)
    try:
        machine = ZMachine(story, host=TerminalHost(ui, args.story), width=ui.width, seed=args.seed)
    except ZMachineError as e:
        ap.error(str(e))

    ai = None
    if not args.no_ai and args.parser == "laya":
        ai = _laya_parser(machine, ui, args)
    elif not args.no_ai:
        client = OllamaClient(model=args.model, url=args.ollama_url)
        try:
            client.resolve_model()
            ai = AIParser(machine, client, debug=ui.debug if args.debug_ai else None)
            ui.notice("[frobai: AI parser using %s — type /help for options]" % client.model)
            threading.Thread(target=_warm, args=(client,), daemon=True).start()
        except LLMError as e:
            ui.notice("[frobai: AI parser disabled — %s]" % e)

    session = Session(machine, ui, ai, show_rewrites=not args.quiet_ai)
    try:
        session.play()
    except ZMachineError as e:
        ui.flush()
        print("\n[frobai: fatal Z-machine error: %s]" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        ui.flush()
    return 0


def _laya_parser(machine, ui, args):
    """Laya rewrites commands; an Ollama model, if one is reachable, answers questions."""
    try:
        from .ai.laya_parser import LayaParser
        from laya import Router
    except ImportError as e:
        ui.notice("[frobai: Laya parser disabled — %s (pip install laya)]" % e)
        return None
    ui.notice("[frobai: loading Laya...]")
    router = Router()
    router.load("english")
    client = OllamaClient(model=args.model, url=args.ollama_url)
    try:
        client.resolve_model()
        threading.Thread(target=_warm, args=(client,), daemon=True).start()
        chat = "questions go to %s" % client.model
    except LLMError:
        client, chat = None, "no Ollama model for questions"
    ui.notice("[frobai: commands rewritten by Laya, %s — type /help for options]" % chat)
    return LayaParser(machine, router=router, chat_client=client, debug=ui.debug if args.debug_ai else None)


def _warm(client):
    try:
        client.warm_up()
    except LLMError:
        pass


if __name__ == "__main__":
    sys.exit(main())
