"""Command rewriting with Laya instead of an LLM.

Laya can't write text, but it can pick from options quickly. So instead of asking a model to write
a command, this asks Laya which action, which object and which direction the player means, choosing
from the game's own vocabulary and the objects actually in scope. The answers are combined into
ranked candidate commands; each call returns the best one not tried yet, and the session checks it
on a snapshot exactly as it does for the LLM parser.

Questions and remarks for the companion still need written answers, so they go to an optional LLM
client; without one, LayaParser only rewrites commands.
"""

import re

from .llm import LLMError
from .parser import AIParser, Reply, strip_prompt

# (key, command template, description). {o} is the object, {i} the second object, {d} a direction.
# A verb is offered only if the first word of its template is in the game's dictionary.
# For two-object verbs, SECOND_ROLE says whether {i} is a target (container, recipient) or a tool.
VERBS = [
    ("take", "take {o}", "pick up, grab, get, collect or carry away a thing"),
    ("drop", "drop {o}", "drop, put down, discard or get rid of a thing being carried"),
    ("open", "open {o}", "open, pry open or unfasten a thing such as a door, box, window or container"),
    ("close", "close {o}", "close, shut or slam a thing"),
    ("examine", "examine {o}", "look at, inspect, check out or study one particular thing closely"),
    ("read", "read {o}", "read the writing on a thing such as a leaflet, book, sign, note or label"),
    ("look", "look", "look around, describe the room, surroundings or where the player is"),
    ("inventory", "inventory", "list what the player is carrying, holding or has with them"),
    ("go", "{d}", "walk, go, run, head, travel or move somewhere in a direction"),
    ("enter", "enter {o}", "go into, get in or climb into a thing such as a boat, house or hole"),
    ("climb", "climb {o}", "climb or scale a thing such as a tree, ladder, rope or wall"),
    ("put_in", "put {o} in {i}", "put, place, stuff or insert a thing inside a container"),
    ("put_on", "put {o} on {i}", "put or set a thing on top of a surface such as a table or altar"),
    ("attack", "attack {o} with {i}", "attack, hit, kill, fight, stab, slash or strike a creature or person"),
    ("light", "turn on {o}", "turn on, switch on, light or activate a thing such as a lamp or lantern"),
    ("extinguish", "turn off {o}", "turn off, switch off, blow out or put out a thing"),
    ("move", "move {o}", "move, push aside, shift, drag or pull a heavy thing such as a rug or rock"),
    ("search", "search {o}", "search, rummage through or look inside a thing"),
    ("eat", "eat {o}", "eat, munch, chew, taste or bite some food"),
    ("drink", "drink {o}", "drink or sip a liquid"),
    ("give", "give {o} to {i}", "give, hand or offer a thing to a person or creature"),
    ("unlock", "unlock {o} with {i}", "unlock a door, grate or chest using a key"),
    ("tie", "tie {o} to {i}", "tie, attach or fasten a rope or cord to a thing"),
    ("throw", "throw {o} at {i}", "throw, toss or hurl a thing at a target"),
    ("wear", "wear {o}", "wear or put on clothing"),
    ("wave", "wave {o}", "wave or brandish a thing in the air"),
    ("ring", "ring {o}", "ring a bell"),
    ("knock", "knock on {o}", "knock or bang on a door"),
    ("listen", "listen", "listen for sounds"),
    ("smell", "smell", "smell or sniff the air"),
    ("wait", "wait", "wait, rest, pause or do nothing for a moment"),
    ("pray", "pray", "pray"),
    ("score", "score", "show the player's score or progress"),
]

SECOND_ROLE = {"put_in": "target", "put_on": "target", "give": "target", "throw": "target", "tie": "target",
               "attack": "tool", "unlock": "tool"}

DIRECTIONS = {
    "north": "north, northward, forward", "south": "south, southward, back",
    "east": "east, eastward", "west": "west, westward",
    "northeast": "northeast", "northwest": "northwest",
    "southeast": "southeast", "southwest": "southwest",
    "up": "up, upward, upstairs, climb up, ascend", "down": "down, downward, downstairs, descend",
    "in": "in, inside, indoors, go inside", "out": "out, outside, outdoors, leave, exit",
}

IGNORED_NAMES = ("you", "yourself", "cretin", "me", "self", "it")
PRONOUNS = ("the", "a", "an", "and", "all", "it", "that", "this", "is", "me", "my", "myself", "i", "im", "you",
            "them", "those", "some", "thing", "stuff")


class NoChat:
    """Stands in for the Ollama client when none is reachable, so /ai still has a model to show."""
    model = "Laya only (no Ollama model for questions)"

    def chat_json(self, *args, **kwargs):
        raise LLMError("no Ollama model for questions")


class LayaParser(AIParser):
    """Drop-in for AIParser: interpret() returns one ranked candidate command per call."""

    def __init__(self, machine, router=None, chat_client=None, max_rounds=6, debug=None, model="english"):
        super().__init__(machine, chat_client or NoChat(), max_rounds=max_rounds, debug=debug)
        if router is None:
            from laya import Router
            router = Router()
        self.router = router
        self.laya_model = model
        self.verbs = [(k, t, d) for k, t, d in VERBS if self._known(t.split()[0].replace("{d}", "north"))]
        self.directions = {d: s for d, s in DIRECTIONS.items() if self._known(d)}

    def _known(self, word):
        return all(addr for _, addr in self.m.lookup_words(word))

    # --- what is in scope -----------------------------------------------------------------
    def _word_at(self):
        if not hasattr(self, "_words_by_addr"):
            self._words_by_addr = {addr: word for addr, word, _ in self.m.dictionary.entries()}
        return self._words_by_addr

    def synonyms(self, o):
        """Dictionary addresses of the object's own nouns: the property made only of dictionary words."""
        m, words = self.m, self._word_at()
        try:
            p = m.first_prop(o)
            while True:
                num, size, data = m.prop_header(p)
                if num == 0:
                    return set()
                if size % 2 == 0:
                    addrs = [m.word(data + k) for k in range(0, size, 2)]
                    if all(a in words for a in addrs):
                        return set(addrs)
                p = data + size
        except Exception:
            return set()

    def _noun(self, name, synonyms=()):
        """The word to use for an object: the last word of its name the game knows, else a synonym."""
        for word in reversed(re.findall(r"[a-z]+", name.lower())):
            addr = self.m.dictionary.lookup(word)
            if addr and (not synonyms or addr in synonyms):
                return word
        words = self._word_at()
        return words[min(synonyms)] if synonyms else None

    def _children(self, obj, depth):
        out, o, guard = [], self.m.child(obj), 0
        while o and guard < 64:
            name = self.m.obj_name(o) or ""
            if name.lower() not in IGNORED_NAMES:
                out.append((o, name))
            if depth > 1:
                out += self._children(o, depth - 1)
            o, guard = self.m.sibling(o), guard + 1
        return out

    def scope(self, line):
        """{noun: description} for things the player could mean."""
        m, things, claimed = self.m, {}, set()
        self.names, self.aliases = {}, {}
        if m.version <= 3:
            try:
                loc = m.word(m.globals)
                if 0 < loc <= 255:
                    for o, name in self._children(loc, 3):
                        parent = m.parent(o)
                        where = "" if parent == loc else " (in or on the %s)" % m.obj_name(parent)
                        if (m.obj_name(parent) or "").lower() in IGNORED_NAMES:
                            where = " (carried by the player)"
                        syn = self.synonyms(o)
                        noun = self._noun(name, syn)
                        if noun and noun not in things:
                            words = self._word_at()
                            others = sorted({words[a] for a in syn} - {noun[:6], noun} - set(name.lower().split()))
                            also = " (also called %s)" % ", ".join(others) if others else ""
                            things[noun] = name + also + where
                            self.names[noun] = name
                            self.aliases[noun] = {w[:6] for w in [noun] + name.lower().split() + others
                                                  + [words[a] for a in syn]}
                            claimed |= syn | {m.dictionary.lookup(noun)}
            except Exception:
                pass
        # Nouns the player typed that the game knows, even if we can't see the object (scenery),
        # unless they are just another name for something already listed.
        skip = set(self.vocab.groups["Verbs"]) | set(self.vocab.groups["Directions"]) | \
            set(self.vocab.groups["Prepositions"]) | set(PRONOUNS)
        for word, addr in m.lookup_words(line)[1:]:  # the first word is almost always the verb
            if addr and addr not in claimed and word.isalpha() and word[:6] not in {w[:6] for w in skip}:
                things.setdefault(word, word)
                self.aliases.setdefault(word, {word[:6]})
                claimed.add(addr)
        return things

    # --- asking Laya ----------------------------------------------------------------------
    # Every question is a yes/no about the player's line alone. On 24 hand-labelled lines, one
    # 33-way "which action?" choice got 7 right; one "Does this command mean: ...?" yes/no per verb
    # got 22 (23 in the top three). Adding the game's recent output to the state made it worse.
    def describe_state(self, line, history, things):
        return line.strip()

    def questions(self, things):
        q = {
            "has_object": {"type": "noul", "instructions": "Does this command name a specific thing to act on?"},
            "two_things": {"type": "noul", "instructions": "Does this command involve two different things, such as "
                                                           "putting one in another, giving one to someone or "
                                                           "using one on another?"},
        }
        for key, _, desc in self.verbs:
            q["verb:" + key] = {"type": "noul", "instructions": "Does this command mean: %s?" % desc}
        for noun, name in things.items():
            q["object:" + noun] = {"type": "noul", "instructions": "Does this command refer to the %s?" % name}
            q["target:" + noun] = {"type": "noul", "instructions": "Is the %s the place, container, person or target "
                                                                   "that something is put in, given to or thrown at?" % name}
            q["tool:" + noun] = {"type": "noul", "instructions": "Is the %s being used as a tool, weapon or key?" % name}
        for d, words in self.directions.items():
            q["direction:" + d] = {"type": "noul", "instructions": "Does this command mean going %s?" % words}
        return q

    def mentions(self, line):
        """Exact matches, which code can see for certain: {noun: "before" or "after"} a preposition."""
        preps = {w[:6] for w in self.vocab.groups["Prepositions"]} | {"in", "into", "on", "onto", "with", "to", "at"}
        side, found = "before", {}
        for word, _ in self.m.lookup_words(line):
            if word[:6] in preps and found:
                side = "after"
            for noun, names in self.aliases.items():
                if word[:6] in names and noun not in found:
                    found[noun] = side
        return found

    def rank(self, answers, things, line=""):
        """All candidate commands, most likely first, as (score, command)."""
        def group(prefix, relative=False):
            g = {k.split(":", 1)[1]: a["noul"] for k, a in answers.items() if k.startswith(prefix + ":")}
            top = max(g.values(), default=0.0)
            return {k: v / top for k, v in g.items()} if relative and top else g
        pv, pd = group("verb"), group("direction")
        # Objects are compared relative to the best one, so a verb that takes an object isn't
        # penalised just because several objects share the probability.
        po, roles = group("object", True), {"target": group("target", True), "tool": group("tool", True)}
        has, two = answers["has_object"]["noul"], answers["two_things"]["noul"]
        # Laya is unsure which object a line names even when the name is right there, so exact
        # matches from code win: a name before a preposition is the object, one after it the second.
        for noun, side in self.mentions(line).items():
            if side == "before":
                po[noun] = 1.0
                for r in roles.values():
                    r[noun] = r.get(noun, 0.0) * 0.3
            else:
                po[noun] = po.get(noun, 0.0) * 0.3
                for r in roles.values():
                    r[noun] = 1.0
                two = max(two, 0.9)
            has = max(has, 0.9)
        cands = {}

        def add(p, cmd):
            cands[cmd] = max(p, cands.get(cmd, 0.0))

        for key, template, _ in self.verbs:
            v = pv.get(key, 0.0)
            if "{d}" in template:
                for d, p in pd.items():
                    add(v * p, d)
            elif "{o}" not in template:
                add(v * (1.0 - has), template)
            elif "{i}" not in template:
                for o, p in po.items():
                    add(v * has * p, template.format(o=o))
            else:
                one_object = template.split(" {i}")[0].rsplit(" ", 1)[0]  # "attack {o}" without "with {i}"
                pi = roles[SECOND_ROLE.get(key, "target")]
                for o, p in po.items():
                    add(v * has * (1.0 - two) * p, one_object.format(o=o))
                    for i, q in pi.items():
                        if i != o:
                            # the moved/attacked thing should not itself look like the target or tool
                            add(v * has * two * p * q * (1.0 - 0.5 * pi.get(o, 0.0)), template.format(o=o, i=i))
        return sorted(((p, c) for c, p in cands.items()), key=lambda pc: -pc[0])

    def interpret(self, line, history, attempts=(), unknown=(), conversational=False):
        if conversational:
            if isinstance(self.client, NoChat):
                return Reply(answer="(Laya parser mode only rewrites commands; start frobai with an "
                                    "Ollama model to ask questions.)")
            return super().interpret(line, history, attempts, unknown, conversational)

        things = self.scope(line)
        state = self.describe_state(line, history, things)
        result = self.router.predict(state, self.questions(things), model=self.laya_model)
        ranked = self.rank(result["answers"], things, line)
        tried = {line.strip().lower()} | {cmd for cmd, _ in attempts}
        if self.debug:
            self.debug("--- laya state ---\n%s\n--- top candidates ---\n%s" % (
                state, "\n".join("%5.1f%%  %s" % (p * 100, c) for p, c in ranked[:8])))
        for p, cmd in ranked:
            if cmd not in tried:
                return Reply([cmd])
        return Reply()


__all__ = ["LayaParser", "LLMError"]
