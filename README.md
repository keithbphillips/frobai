# frobai

A Z-machine interpreter for Infocom and Inform story files, with a parser helped by a small local AI model.

The classic parsers are strict. `grab the paper thing that is in the box` gets you `I don't know the word "grab"`. frobai sits between the player and the game, rewrites what you typed into commands the game understands, and checks that the game really accepted them:

```
>grab the paper thing that is in the box
» open mailbox
Opening the small mailbox reveals a leaflet.

>» take leaflet
Taken.

>pry the window open
» open window
With great effort, you open the window far enough to allow entry.
```

It's also a companion you can talk to. Questions get a short answer based on what's happened so far, and they don't use up a game turn:

```
>where am I?
You're standing west of a white house, next to a small mailbox you've already opened.

>what am I doing here, exactly?
So far: you've read a leaflet welcoming you to Zork. Beyond that, the story hasn't told you.

>any ideas?
The boarded front door looks unpromising. Walking around the house might turn up another way in.
```

If a line looks like a question (it ends in `?`, or starts with words like "where", "what" or "any ideas"), it goes to the companion. Start a line with `?` to always talk to the companion. Answers are based only on the transcript and status line, not on outside knowledge of the game, and hints are nudges, not solutions.

## Running

```sh
python -m frobai path/to/zork1.z3            # or: pip install -e . && frobai zork1.z3
python -m frobai story.z5 --model qwen3:4b   # choose the Ollama model
python -m frobai story.z5 --no-ai            # plain interpreter
```

The AI needs [Ollama](https://ollama.com) running locally. By default frobai uses the smallest installed model; set `--model` or `FROBAI_MODEL` to pick one, and `--ollama-url` / `OLLAMA_HOST` to point at another server. If Ollama can't be reached, frobai tells you and runs as a plain interpreter.

While playing:

| Command | Effect |
|---|---|
| `?TEXT` | Ask the companion (no game turn) |
| `/ai on`, `/ai off` | Turn the AI parser on or off |
| `/ai` | Show the model and vocabulary |
| `/raw TEXT` | Send TEXT to the game without the AI |
| `/debug` | Show AI prompts and responses |
| `/quit` | Leave immediately |

Other options: `--quiet-ai` (hide substituted commands), `--seed N` (repeatable randomness), `--width N`, `--no-status`, `--no-color`.

## How the AI parser works

The AI parser tries each command on a copy of the game first.

1. **Check the words.** Split the input with the game's own dictionary and word separators. If every word is known, go to step 2. If any word is unknown, skip to step 3.
2. **Try the command.** Take a snapshot of the machine (dynamic memory, call stack, screen and random-number state), run the command and look at the reply. If it isn't a parser rejection ("I don't know the word…", "That's not a verb I recognise.", "You can't see any such thing."…), it counts as the player's move.
3. **Rewrite it.** If the game rejected the command, restore the snapshot. Then send the model:
   - the game's dictionary, grouped by verbs, directions and prepositions when the dictionary flags show them
   - the recent transcript
   - the objects in the player's location (v1–3 games)
   - any earlier attempts, with the game's reply to each

   The model returns JSON: `{"commands": [...]}`. If its answer uses words that aren't in the dictionary, it gets one more try.
4. **Check the rewrite.** Each rewrite is run on a snapshot too. If the game rejects it, it's rolled back and the model gets the rejection for another round (up to 3). Only commands the game accepts are shown and kept.
5. **Fall back.** If nothing works, the game answers what the player actually typed. The one exception: if a rewrite got a more useful reply (such as "You can't see any table here!"), that reply is shown.

As a result, the AI never changes the game state through a command the game didn't accept. It doesn't take turns you didn't ask for, and it doesn't see anything the player can't.

## Interpreter

- Versions 1–5, 7 and 8, following Z-Machine Standard 1.1. Version 6 graphics games are not supported.
- Passes the Czech compliance suite (v3, v4, v5, v8) and every Praxix test.
- Quetzal (`.qzl`) save files, which work with Frotz and other interpreters. Also `save_undo`/`restore_undo`, output streams 1–3, the upper window and the status line.
- Pure Python standard library, no dependencies.

## Tests

```sh
python -m unittest discover -s tests -v
```

The Czech and Praxix test story files are in `tests/stories`. The Zork tests use `~/github/zork1/COMPILED/zork1.z3`, or the path in `FROBAI_ZORK1`, and are skipped if that file isn't there. The AI session tests use a fake model, so they don't need Ollama.

## Layout

```
frobai/zmachine/   machine.py (CPU, objects, I/O opcodes), text.py (ZSCII, dictionary),
                   screen.py (window model), quetzal.py (save files)
frobai/ai/         llm.py (Ollama client), parser.py (failure detection, vocabulary, prompts)
frobai/session.py  speculative execution and rewrite loop
frobai/ui.py       terminal frontend
frobai/cli.py      command line
```
