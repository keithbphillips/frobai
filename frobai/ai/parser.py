"""AI-assisted parser: detects parser rejections and rewrites player input with a local LLM."""

import re

from .llm import LLMError

# Parser rejections from the Infocom (ZIL) and Inform 5/6/7 libraries. Lowercased substrings/regexes.
FAILURE_PATTERNS = [re.compile(p) for p in (
    r"i don'?t know the word",
    r"you used the word .* in a way that i don'?t understand",
    r"there was no verb in that sentence",
    r"that sentence isn'?t one i recogni[sz]e",
    r"i don'?t understand (that|what)",
    r"^i beg your pardon",
    r"i found too many nouns",
    r"there were too many nouns",
    r"you can'?t use multiple",
    r"that'?s not a verb i recogni[sz]e",
    r"i didn'?t understand (that sentence|the way that finished|that number)",
    r"i only understood you as far as",
    r"you can'?t see any such thing",
    r"you seem to have said too little",
    r"that'?s not something you need to refer to",
    r"i'?m not sure what .* refers to",
    r"you excepted something not included",
    r"that noun did not make sense",
    r"you can'?t see any .* here",
    r"i don'?t know what .* refers to",
    r"^huh\?",
    r"^what\?$",
    r"i don'?t recogni[sz]e that",
    r"(that|this) verb is not recogni[sz]ed",
)]

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "commands": {"type": "array", "items": {"type": "string"}},
        "answer": {"type": "string"},
    },
    "required": ["commands", "answer"],
}

SYSTEM_TEMPLATE = """You are frobai, a friendly companion built into an interpreter for classic text adventure games. You sit between the player and the game's parser, which is old and strict: it only understands short imperative commands made from its own vocabulary, for example:
  north / n / up / in
  take lamp
  open the mailbox
  put sword in case
  examine leaflet
  inventory / look / score

Each time you are called, decide what the player wants.

A) AN ACTION IN THE GAME. Translate it into parser commands in "commands":
- Use ONLY words from the game vocabulary below. The parser only compares the first {n} letters of each word, so a longer word is fine if its first {n} letters match a vocabulary entry.
- Refer only to things the player mentioned or that appear in the transcript. Do not invent objects or add actions the player did not ask for.
- Prefer a single command. Use several only when the player clearly asked for several separate actions, in the order they should happen.
- Map synonyms onto vocabulary words (e.g. "grab" or "pick up" -> "take", "look at" -> "examine", "go inside" -> "in").
- Do not guess at the map: only move in a direction the player named or that the transcript makes obvious.
- If an earlier attempt failed, the game's response tells you why; use different wording and never repeat a failed command.

B) A QUESTION OR REMARK FOR YOU, such as "where am I?", "what am I doing?", "what did the leaflet say?", "any ideas?". Reply in "answer":
- 1 to 3 short sentences, in the second person, in the dry, wry voice of a classic Infocom narrator. Be specific and a little playful; don't pad.
- Speak as a companion who has been watching the story, never about "the transcript", "the status line" or "the game text".
- Use ONLY what the transcript and status line show. Pretend you have never seen this game before: no outside knowledge, maps or walkthroughs. If you don't know, say so and suggest how to find out.
- If the player asks for help or ideas, point out things from the transcript worth examining, opening, or trying, as gentle nudges. Never solve a puzzle outright.
- Leave "commands" empty unless the transcript can't answer the question and one harmless command would (look, inventory, examine something, score).

If you can neither translate nor answer, leave "commands" empty and use "answer" to tell the player briefly what the game didn't understand and suggest another way to say it.

Respond with JSON only: {{"commands": ["..."], "answer": "..."}}. Use "" for "answer" when you have nothing to say.

GAME VOCABULARY
{vocabulary}"""

WORD_RE = re.compile(r"^[a-z][a-z'-]*$")

QUESTION_RE = re.compile(
    r"^(?:(?:what|where|who|why|how|when|which)(?:'s|'re|\b)"
    r"|(?:am|are|is|was|were|do|does|did|can|could|should|would|will|have|has)\s+"
    r"(?:i|we|there|it|this|that|you|my)\b"
    r"|any\s+(?:hints?|ideas?|clues?|suggestions?|tips?)\b"
    r"|(?:help me|remind me|tell me|i'm stuck|i am stuck|i'm lost|i am lost)\b)", re.I)


def looks_conversational(line):
    """Heuristic: is this a question or remark for the companion rather than a game command?"""
    s = line.strip()
    return s.endswith("?") or bool(QUESTION_RE.match(s))


class Reply:
    def __init__(self, commands=(), answer=""):
        self.commands = list(commands)
        self.answer = answer

    def __repr__(self):
        return "Reply(commands=%r, answer=%r)" % (self.commands, self.answer)


def strip_prompt(output):
    """Remove the trailing input prompt (e.g. '>') from game output."""
    lines = output.rstrip().split("\n")
    if lines and len(lines[-1].strip()) <= 2 and not lines[-1].strip().isalnum():
        lines.pop()
    return "\n".join(lines).strip()


def is_parser_failure(output):
    text = strip_prompt(output).lower()
    if not text or len(text) > 400:
        return False
    return any(p.search(text) for p in FAILURE_PATTERNS)


# Rejections where the sentence parsed but refers to something that isn't present.
WORLD_REFUSAL = re.compile(r"(you|i) can'?t see any|you don'?t see any|there is no .* here")


def is_world_refusal(output):
    text = strip_prompt(output).lower()
    return bool(text) and len(text) <= 400 and bool(WORLD_REFUSAL.search(text))


class Vocabulary:
    """The game's dictionary, grouped by part of speech where the story format lets us tell."""

    def __init__(self, machine):
        self.m = machine
        d = machine.dictionary
        mem = machine.mem
        signature = bytes(mem[0x3C:0x40])
        self.style = "unknown"
        if all(0x20 < b < 0x7F for b in signature):
            self.style = "inform"
        elif machine.version <= 3 and d.entry_length >= 7:
            self.style = "infocom"
        self.groups = {"Verbs": [], "Directions": [], "Prepositions": [], "Other words": []}
        self.words = []
        for _, word, data in d.entries():
            if not WORD_RE.match(word):
                continue
            self.words.append(word)
            flags = data[0] if data else 0
            if self.style == "infocom":
                if flags & 0x40:
                    group = "Verbs"
                elif flags & 0x10:
                    group = "Directions"
                elif flags & 0x08:
                    group = "Prepositions"
                else:
                    group = "Other words"
            elif self.style == "inform":
                group = "Verbs" if flags & 0x01 and not flags & 0x80 else "Other words"
            else:
                group = "Other words"
            self.groups[group].append(word)

    def describe(self):
        lines = []
        for name, words in self.groups.items():
            if words:
                label = name if self.style != "unknown" else "Words"
                lines.append("%s: %s" % (label, " ".join(sorted(set(words)))))
        return "\n".join(lines)


class AIParser:
    def __init__(self, machine, client, max_rounds=3, history_chars=2500, conversation_chars=7000,
                 debug=None):
        self.m = machine
        self.client = client
        self.max_rounds = max_rounds
        self.history_chars = history_chars
        self.conversation_chars = conversation_chars
        self.enabled = True
        self.debug = debug              # optional callable(str) for prompt/response logging
        self.vocab = Vocabulary(machine)
        self._system = None

    is_failure = staticmethod(is_parser_failure)
    looks_conversational = staticmethod(looks_conversational)

    @property
    def system_prompt(self):
        if self._system is None:
            n = 6 if self.m.version <= 3 else 9
            self._system = SYSTEM_TEMPLATE.format(n=n, vocabulary=self.vocab.describe())
        return self._system

    def unknown_words(self, line):
        return [w for w, addr in self.m.lookup_words(line)
                if addr == 0 and not w.isdigit() and any(c.isalpha() for c in w)]

    def location_objects(self):
        """Names of objects in the player's location (v1-3 only, where the location global is fixed)."""
        m = self.m
        if m.version > 3:
            return []
        try:
            loc = m.word(m.globals)
            if not 0 < loc <= 255:
                return []
            names, o, guard = [], m.child(loc), 0
            while o and guard < 64:
                name = m.obj_name(o)
                if name and name.lower() not in ("you", "yourself", "cretin", "me", "self"):
                    names.append(name)
                o, guard = m.sibling(o), guard + 1
            return names
        except Exception:
            return []

    def status_line(self):
        scr = self.m.screen
        if self.m.version <= 3:
            return "%s   %s" % scr.status if scr.status else ""
        rows = [r.strip() for r in scr.upper_text() if r.strip()]
        return " | ".join(rows[:2])

    def transcript(self, history, limit):
        parts = []
        total = 0
        for cmd, out in reversed(history):
            chunk = (">%s\n" % cmd if cmd else "") + strip_prompt(out)
            if total + len(chunk) > limit and parts:
                break
            parts.append(chunk)
            total += len(chunk)
        text = "\n\n".join(reversed(parts))
        return text[-limit:]

    def build_user_prompt(self, line, history, attempts, unknown, conversational=False):
        limit = self.conversation_chars if conversational else self.history_chars
        sections = ["Transcript so far (most recent last; lines starting [frobai] are your own earlier "
                    "answers):\n" + (self.transcript(history, limit) or "(none)")]
        status = self.status_line()
        if status:
            sections.append("Status line: " + status)
        objs = self.location_objects()
        if objs:
            sections.append("Objects in the current location: " + ", ".join(objs))
        sections.append('Player typed: "%s"' % line.strip())
        if conversational:
            sections.append("This looks like a question or remark for you rather than a game command.")
        elif not attempts and not unknown:
            sections.append("The game's parser could not handle it.")
        if unknown:
            sections.append("Words in that input the game does not know: " + ", ".join(unknown))
        if attempts:
            sections.append("Commands already tried, with the game's response:\n" + "\n".join(
                "  >%s\n  %s" % (cmd, strip_prompt(resp).replace("\n", " ")) for cmd, resp in attempts))
        sections.append("Respond with the JSON object.")
        return "\n\n".join(sections)

    @staticmethod
    def clean(cmd):
        cmd = cmd.strip().strip('"').strip()
        cmd = re.sub(r"^>+\s*", "", cmd)
        return cmd.rstrip(".!").lower().strip()

    def _ask(self, user):
        if self.debug:
            self.debug("--- prompt ---\n" + user)
        resp = self.client.chat_json(self.system_prompt, user, RESPONSE_SCHEMA)
        if self.debug:
            self.debug("--- response ---\n%r" % resp)
        if not isinstance(resp, dict):
            return Reply()
        cmds = resp.get("commands") or []
        answer = resp.get("answer")
        return Reply([c for c in (self.clean(x) for x in cmds if isinstance(x, str)) if c],
                     answer.strip() if isinstance(answer, str) else "")

    def interpret(self, line, history, attempts=(), unknown=(), conversational=False):
        """Ask the model what the player means. Returns a Reply; raises LLMError if unreachable."""
        user = self.build_user_prompt(line, history, list(attempts), list(unknown), conversational)
        reply = self._ask(user)
        bad = sorted({w for c in reply.commands for w in self.unknown_words(c)})
        if bad:
            user += ("\n\nYour previous commands %s used words that are NOT in the game vocabulary: %s. "
                     "Use vocabulary words only." % (reply.commands, ", ".join(bad)))
            retry = self._ask(user)
            if retry.commands or retry.answer:
                reply = retry
        return reply


__all__ = ["AIParser", "LLMError", "Reply", "Vocabulary", "is_parser_failure", "is_world_refusal",
           "looks_conversational", "strip_prompt"]
