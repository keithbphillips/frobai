"""Game loop tying the Z-machine, the frontend and the AI parser together.

Every player command is first run *speculatively*: the machine is snapshotted, the command is
executed, and if the game's own parser rejects it the snapshot is restored and the AI parser is
asked for a rewrite. Rewrites are verified the same way before they are shown to the player, so the
game state only ever advances by commands the game actually accepted.
"""

from .ai.llm import LLMError
from .ai.parser import is_world_refusal
from .zmachine import HALTED, WAIT_CHAR, WAIT_LINE

HELP = """Talk to frobai: ask questions like "where am I?", "what am I doing?" or "any ideas?".
Start a line with ? to always talk to frobai instead of the game (e.g. "?what was that noise").

frobai commands:
  /ai on|off     enable or disable the AI parser
  /ai            show AI parser status
  /raw TEXT      send TEXT to the game without AI assistance
  /debug         toggle printing of AI prompts and responses
  /help          this help
  /quit          leave immediately (use the game's SAVE first!)"""


class Session:
    def __init__(self, machine, ui, ai=None, show_rewrites=True):
        self.m = machine
        self.ui = ui
        self.ai = ai
        self.show_rewrites = show_rewrites
        self.history = []

    # --- primitives -----------------------------------------------------------------------
    def _run(self):
        self.m.run()
        return self.m.screen.take_output()

    def execute(self, cmd):
        self.m.provide_line(cmd)
        return self._run()

    def commit(self, cmd, out, rewritten=False):
        self.history.append((cmd, out))
        del self.history[:-40]
        if rewritten and self.show_rewrites:
            self.ui.command_echo(cmd)
        self.ui.show(out)

    def speculate(self, cmd):
        """Run cmd; if the game's parser rejects it, roll back. Returns (accepted, output)."""
        snap = self.m.snapshot()
        out = self.execute(cmd)
        if self.m.state == WAIT_LINE and self.ai.is_failure(out):
            self.m.restore_snapshot(snap)
            return False, out
        return True, out

    # --- main loop ------------------------------------------------------------------------
    def play(self):
        self.commit(None, self._run())
        while self.m.state != HALTED:
            self.ui.show_upper(self.m)
            if self.m.state == WAIT_CHAR:
                key = self.ui.read_key()
                if key is None:
                    break
                self.m.provide_char(key)
                self.commit(None, self._run())
                continue
            line = self.ui.read_line()
            if line is None:
                break
            if line.startswith("/") and self.meta(line):
                continue
            self.handle_line(line)
        self.ui.flush()

    def meta(self, line):
        """Handle /commands. Returns False if the line should go to the game instead."""
        word, _, rest = line[1:].partition(" ")
        word = word.lower()
        ui = self.ui
        if word == "raw":
            self.commit(rest, self.execute(rest))
            return True
        if word == "ai":
            if not self.ai:
                ui.notice("AI parser is not available (started with --no-ai or Ollama unreachable).")
            elif rest.strip().lower() in ("on", "off"):
                self.ai.enabled = rest.strip().lower() == "on"
                ui.notice("AI parser %s." % ("enabled" if self.ai.enabled else "disabled"))
            else:
                ui.notice("AI parser: %s, model %s, vocabulary %d words (%s dictionary)." % (
                    "on" if self.ai.enabled else "off", self.ai.client.model, len(self.ai.vocab.words),
                    self.ai.vocab.style))
        elif word == "debug":
            if self.ai:
                self.ai.debug = None if self.ai.debug else ui.debug
                ui.notice("AI debug output %s." % ("on" if self.ai.debug else "off"))
        elif word in ("help", "?"):
            ui.notice(HELP)
        elif word in ("quit", "exit"):
            self.m.state = HALTED
        else:
            return False
        return True

    def handle_line(self, line):
        m, ai = self.m, self.ai
        if not ai or not ai.enabled or not line.strip():
            return self.commit(line, self.execute(line))

        stripped = line.strip()
        if stripped.startswith("?"):
            line = stripped[1:].strip() or "any ideas?"
            conversational = True
        else:
            conversational = ai.looks_conversational(stripped)

        unknown = [] if conversational else ai.unknown_words(line)
        failure = None
        snap = m.snapshot()
        if not conversational and not unknown:
            accepted, out = self.speculate(line)
            if accepted:
                return self.commit(line, out)
            failure = out

        if self.consult_ai(line, failure, unknown, conversational):
            return
        # Nothing better was found: let the game respond to what the player actually typed.
        m.restore_snapshot(snap)
        self.commit(line, self.execute(line))

    def narrate(self, line, answer):
        """Show a companion answer. It costs no game turn but is remembered for follow-up questions."""
        self.history.append((line, "[frobai] " + answer))
        del self.history[:-40]
        self.ui.narrate(answer)

    def consult_ai(self, line, failure, unknown, conversational=False):
        ai = self.ai
        attempts = [(line, failure)] if failure is not None else []
        tried = {line.strip().lower()}
        refused = None     # a rewrite the game understood but refused ("You can't see any X here")
        for _ in range(ai.max_rounds):
            self.ui.thinking(True)
            try:
                reply = ai.interpret(line, self.history, attempts, unknown, conversational)
            except LLMError as e:
                self.ui.notice("[AI parser unavailable: %s]" % e)
                break
            finally:
                self.ui.thinking(False)
            cmds = reply.commands
            if not cmds or cmds[0] in tried:
                if reply.answer:
                    self.narrate(line, reply.answer)
                    return True
                break
            tried.add(cmds[0])

            accepted, out = self.speculate(cmds[0])
            if not accepted:
                attempts.append((cmds[0], out))
                if refused is None and is_world_refusal(out):
                    refused = cmds[0]
                continue
            if reply.answer:
                self.narrate(line, reply.answer)
            self.commit(cmds[0], out, rewritten=True)
            self.run_followups(cmds[1:])
            return True

        # The player's own words meant nothing to the game, but a rewrite got a meaningful refusal:
        # that refusal ("You can't see any table here") is more useful than "I don't know the word".
        if refused is not None and (failure is None or not is_world_refusal(failure)):
            self.commit(refused, self.execute(refused), rewritten=True)
            return True
        return False

    def run_followups(self, cmds):
        for cmd in cmds:
            if self.m.state != WAIT_LINE:
                return
            accepted, out = self.speculate(cmd)
            if not accepted and not is_world_refusal(out):
                self.ui.notice("(skipped “%s”: the game didn't understand it)" % cmd)
                return
            self.commit(cmd, out, rewritten=True)
            if not accepted:
                return
