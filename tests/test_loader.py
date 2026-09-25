from __future__ import annotations

from djplusai.backends.sim import SimBackend
from djplusai.library import Track
from djplusai.loader import LibrarySearchLoader, search_query

WANTED = Track(1, "Drake & Future", "Life Is Good", duration=237.0, bpm=142.0)
REMIX = Track(2, "Drake & Future", "Life Is Good (Remix)", duration=250.0, bpm=142.0)


class FakeTypist:
    def __init__(self) -> None:
        self.typed: list[str] = []
        self.activated = 0

    async def activate(self) -> None:
        self.activated += 1

    async def replace_text(self, text: str) -> None:
        self.typed.append(text)


class LibraryUI(SimBackend):
    """Simulated Mixxx library view: a result list, a selection cursor and LoadSelectedTrack."""

    def __init__(self, results: list[Track]) -> None:
        super().__init__()
        self.results = results
        self.row = -1
        self.focus_log: list[float] = []

    def _set(self, group, key, v, log=True):
        if group == "[Library]" and key == "focused_widget":
            self.focus_log.append(v)
        elif group == "[Library]" and key == "MoveDown" and v > 0:
            self.row = (self.row + 1) % len(self.results)
        elif key == "LoadSelectedTrack" and v > 0:
            deck = self.decks[int(group[-2])]
            deck.track, deck.pos = self.results[self.row], 0.0
        else:
            super()._set(group, key, v, log)


def test_search_query_is_precise():
    assert search_query(Track(1, 'A "B"', "C")) == 'title:"C" artist:"A  B"'


async def test_loader_steps_past_wrong_results_and_verifies():
    ui = LibraryUI([REMIX, WANTED])
    typist = FakeTypist()
    res = await LibrarySearchLoader(ui, typist).load(1, WANTED)
    assert res.ok and res.verified, res
    assert typist.activated == 1 and typist.typed == ['title:"Life Is Good" artist:"Drake & Future"']
    assert ui.focus_log == [1, 3]  # search box, then the track table
    assert ui.decks[1].track is WANTED


async def test_loader_refuses_playing_deck():
    ui = LibraryUI([WANTED])
    await ui.load_track(1, REMIX)
    await ui.set("[Channel1]", "play", 1)
    res = await LibrarySearchLoader(ui, FakeTypist()).load(1, WANTED)
    assert not res.ok and "playing" in res.message


async def test_loader_gives_up_when_track_never_appears():
    ui = LibraryUI([REMIX])
    res = await LibrarySearchLoader(ui, FakeTypist(), max_candidates=2).load(2, WANTED)
    assert not res.ok
