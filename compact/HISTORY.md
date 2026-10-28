# Why compact has the walls it has

compact moves text from a working file into an archive. By hand, that is a ritual of several steps: copy
the block, hash it, paste it into the archive, hash the archive's copy, delete the block, note the
ordinal. Done many times by different people, the ritual damaged files in a few repeatable ways, and each
wall below was added the first time its kind of damage was seen, usually with every earlier check green.
This is the reason for each. The rule inside the code is one or two sentences; the story is here.

## The invariant: the archive is verbatim and append-only

The archive is the only place the moved text now lives, so nothing in compact may delete from it or edit a
block in it. That is a property of the tool, not an option. Rolling back a failed append (wall 3) is not a
deletion: nothing that was ever proven archived is removed.

## Wall 1: anchors are content, never line numbers

A line number goes stale the moment the file changes, and every file this is aimed at changes. An anchor
that matches two lines and is resolved to "the first" is the worst failure mode available: the tool picks
which of two identical lines you meant. So an anchor must match exactly one line, compared whole and
exactly, or the run is refused.

## Wall 2: the block is cut at a blank line or not cut

The usual hand error is a boundary drawn one line off. It leaves a bullet cut mid-sentence in the source
and an orphan in the archive. Both look fine locally. The wall is structural (the lines either side of the
block are blank, or the block is at the edge of the file) and is independent of the digest, so someone who
computed no digest still gets it.

## Wall 3: the archive is proven before the source is touched

A digest taken once proves nothing about what was written. The digest is taken from the source block, the
archive is appended, the archive is re-read from disk and the archived copy is digested again; only then
is the source edited. A mount or a write that mangles one byte is caught before the only other copy is
removed. If it is caught, the archive goes back to the bytes it had and the source was never opened.

## Wall 4: the ordinal is read and printed, not written

Archive headings carry an ordinal ("SEVENTIETH"). Taking the next one by counting headings, or by copying
the previous entry's number, produced duplicates, because headings are not kept in file order and one pass
can carry two. The pattern is run, the highest ordinal is reported with the next one, and a word the table
cannot name is reported rather than guessed. compact never writes the number into prose: the stub
paragraph is a sentence someone chose, and refreshing it in place is a judgment about the sentence.

## Wall 5: a divider that names the marker word must be readable by the pattern

A heading that mentions the marker word in passing, but is spelled so the ordinal pattern cannot parse it,
is invisible to the one method that settles the sequence. The first real use of the tool wrote exactly such
a heading and a separate check caught it. The rule moved into the tool, at the point of writing, where the
fix costs one word: spell an ordinal the pattern reads, or leave the word out. There is no override flag.

## Wall 6: `--end` is inclusive

Someone gave the next entry as `--end`, reading the range as half-open. The block was still blank-line
bounded, the digest before and after matched, and every signal was green; what moved was a finished entry
plus a still-open one, which must never be archived. A digest proves the block moved faithfully, never that
it was the right block. Making `--end` exclusive would have silently re-aimed every command already typed,
so the wall refuses instead. A one-line entry (start equal to end) is exempt because that is the commonest
legitimate use, and the opening line only counts if it follows a blank, so a nested list opens nothing.

## Wall 7: source and archive must be different files

Pointed at one file that held live entries above a divider and an archive below it, the tool read both
texts, appended to the archive, proved the append green, and then wrote the source from the original text
minus the block, over the append it had just proven. The block was deleted, nothing was archived, and every
line of output was green. The wall fires before any byte is read, by resolved path and by inode. A
same-file move is a different program and stays a hand move.

## Wall 8: `--bullet` moves exactly one top-level bullet

Long narratives often live as adjacent bullets with no blank line between them, so wall 2 refuses them by
construction. `--bullet` replaces wall 2 with three structural checks: the start opens a top-level item,
no other line of the block does (an end aimed at the next bullet is refused, wall 6's hazard without the
blank), and the line after the block is blank, a top-level item, a heading or the end of the file. An
indented item after the block belongs to the bullet before it, and cutting in front of it is a
decapitation. `--with FILE` can put a shortened replacement where the bullet stood; it is read from a file
so quotes survive, and only in this mode, so the default mode still rewrites nothing.

## Wall 9: a drain's stub cites its divider

A stub left behind names what it replaced. Measured over a large set of stubs, the stub's own nickname
for the entry matched the archive's heading badly (about half missed it), and matching the divider instead
missed more often, because one divider often covers a whole batch. Both failed for the same reason: the
stub's words and the archive's words were never the same string. So the drain path stops trusting a match
and writes one: the stub ends with `` body under `<divider>`. ``, the divider quoted byte for byte, which
anything can then resolve against the archive without guessing. The prose before it stays the caller's.
The citation must be last so it is always in the same place to read.

## `--verify`: the proof compact cannot give itself

compact proves a move at the moment it makes it. It cannot know what happens afterwards: a hand edit to
the archive or the source, a merge that reorders lines, a stub reworded until it no longer cites its
divider. `--verify` checks the result against git, after the commit if you like, with the same generic
checks regardless of who or what made the change: the archive only grew; what the source lost is what the
archive gained, verbatim and in order; every stub that cites a divider cites one that exists; and putting
the lost lines back rebuilds the base. It is the after-commit counterpart of wall 3.

## Line endings

A file checked out on Windows may carry CRLF. Matching anchors, moving lines and writing back must not
change a file's endings, so each file is read without translation, matched with its own endings set aside,
and written back with them. A file that mixes the two cannot be moved verbatim between files with a clear
meaning, so it is refused until it is made consistent.
