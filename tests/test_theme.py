"""Tests for theming.

Every colour flows through custom properties, which is what makes a second
theme a token block rather than a sweep through the stylesheet. These tests
guard that property, since one hard-coded hex re-introduces the sweep.
"""
import os
import re

import pytest

from paths import INDEX_HTML as INDEX




@pytest.fixture(scope="module")
def css():
    with open(INDEX) as f:
        return f.read().split("<style>", 1)[1].split("</style>", 1)[0]


TOKENS = ["--ground", "--panel", "--panel-2", "--line", "--ink", "--ink-2",
          "--muted", "--accent", "--accent-soft", "--accent-2",
          "--on-accent"]


def _tokens_in(css, marker):
    """Colour tokens only -- :root also carries the font stacks, which the
    theme override blocks have no reason to repeat."""
    i = css.index(marker)
    found = re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", css[i:css.index("}", i)])
    return {k: v.strip() for k, v in found if v.strip().startswith("#")}


class TestTheDuplicatedBlocksAgree:
    """The palette is written four times -- :root, the prefers-color-scheme
    block, and the two explicit overrides -- because CSS needs it that way.
    Four copies drift, and the two that drifted unnoticed were the two nobody
    tests: :root is what renders before any choice is made, and the media
    query is what an OS-light user sees.
    """

    def test_root_matches_the_explicit_dark_block(self, css):
        assert _tokens_in(css, ":root {") == _tokens_in(css, ':root[data-theme="dark"] {')

    def test_the_media_query_matches_the_explicit_light_block(self, css):
        assert _tokens_in(css, ':root:not([data-theme="dark"]) {') == \
               _tokens_in(css, ':root[data-theme="light"] {')


class TestNoDeadTokens:
    def test_every_token_is_used(self, css):
        """A token nothing references still has to be kept contrast-correct
        in two themes forever. --live and --stop sat unused after a rewrite
        dropped the class that referenced them."""
        block = css.split(":root {", 1)[1].split("}", 1)[0]
        defined = set(re.findall(r"(--[a-z0-9-]+):", block))
        rules = css[css.rindex(':root[data-theme="dark"]'):]
        used = set(re.findall(r"var\((--[a-z0-9-]+)\)", css))
        unused = {t for t in defined if t not in used and not t.startswith(("--mono", "--sans"))}
        assert not unused, f"defined but never referenced: {sorted(unused)}"


class TestTokenCoverage:
    @pytest.mark.parametrize("token", TOKENS)
    def test_defined_in_dark(self, css, token):
        block = css.split(":root {", 1)[1].split("}", 1)[0]
        assert token + ":" in block

    @pytest.mark.parametrize("token", TOKENS)
    def test_defined_in_light(self, css, token):
        block = css.split(':root[data-theme="light"] {', 1)[1].split("}", 1)[0]
        assert token + ":" in block, f"{token} has no light value"

    def test_light_and_dark_define_the_same_set(self, css):
        def names(marker):
            block = css.split(marker, 1)[1].split("}", 1)[0]
            return set(re.findall(r"(--[a-z0-9-]+):", block))
        light = names(':root[data-theme="light"] {')
        dark = names(':root[data-theme="dark"] {')
        assert light == dark, f"asymmetric: {light ^ dark}"

    def test_no_hard_coded_colours_outside_the_token_blocks(self, css):
        """One stray hex is one thing that stays dark on a light page."""
        rules = css[css.rindex(':root[data-theme="dark"]'):]
        rules = rules[rules.index("}") + 1:]
        stray = re.findall(r"#[0-9A-Fa-f]{3,8}\b", rules)
        assert not stray, f"hard-coded colours: {stray}"


class TestThemeBehaviour:
    def test_follows_the_os_by_default(self, css):
        assert "@media (prefers-color-scheme: light)" in css

    def test_an_explicit_choice_overrides_the_os_both_ways(self, css):
        """Picking light on a dark-mode phone must actually do something."""
        assert ':root[data-theme="light"]' in css
        assert ':root[data-theme="dark"]' in css
        # The media query must not beat an explicit dark choice.
        media = css.split("@media (prefers-color-scheme: light)", 1)[1]
        assert ':root:not([data-theme="dark"])' in media.split("}", 1)[0]

    @pytest.mark.parametrize("marker,expected", [
        (":root {", "dark"),
        (':root:not([data-theme="dark"]) {', "light"),
        (':root[data-theme="light"] {', "light"),
        (':root[data-theme="dark"] {', "dark"),
    ])
    def test_every_theme_block_declares_color_scheme(self, css, marker, expected):
        """Without it the native time, number and range controls render as
        dark widgets on a light page.

        Checked per block, not as a substring anywhere in the file: with four
        blocks, three can lose the declaration and a naive `in css` still
        passes."""
        block = css.split(marker, 1)[1].split("}", 1)[0]
        assert f"color-scheme: {expected}" in block, f"{marker} does not set it"

    def test_choice_is_persisted(self):
        markup = open(INDEX).read()
        assert "localStorage.setItem('dj-theme'" in markup
        assert "localStorage.getItem('dj-theme')" in markup

    def test_storage_failure_does_not_break_the_page(self):
        """Safari private browsing throws on localStorage."""
        markup = open(INDEX).read()
        block = markup.split("function currentTheme(", 1)[1].split("\n  }", 1)[0]
        assert "catch" in block

    def test_theme_applied_before_the_rest_boots(self):
        markup = open(INDEX).read()
        boot = markup.rsplit("// ---- Boot", 1)[1]
        assert boot.index("applyTheme(") < boot.index("refreshNowPlaying()")

    def test_accents_differ_between_themes(self, css):
        """A pale ground needs a darker accent or the contrast fails, so the
        two themes cannot share accent values."""
        def token(marker, name):
            block = css.split(marker, 1)[1].split("}", 1)[0]
            return re.search(name + r":\s*([^;]+);", block).group(1).strip()
        for name in ("--accent", "--accent-2", "--on-accent"):
            assert token(':root[data-theme="light"] {', name) != \
                   token(':root[data-theme="dark"] {', name), name

    def test_the_two_accents_are_distinct(self, css):
        """A second accent that matches the first is not a second accent."""
        def token(marker, name):
            block = css.split(marker, 1)[1].split("}", 1)[0]
            return re.search(name + r":\s*([^;]+);", block).group(1).strip()
        for marker in (":root {",
                       ':root:not([data-theme="dark"]) {',
                       ':root[data-theme="light"] {',
                       ':root[data-theme="dark"] {'):
            assert token(marker, "--accent") != token(marker, "--accent-2"), marker


class TestContrast:
    """A theme that only the author can read is not a theme. WCAG AA for
    normal text is 4.5:1."""

    @staticmethod
    def _ratio(fg, bg):
        def lum(h):
            h = h.lstrip("#")
            r, g, b = (int(h[i:i+2], 16) / 255 for i in (0, 2, 4))
            f = lambda c: c/12.92 if c <= 0.03928 else ((c + 0.055)/1.055) ** 2.4
            return 0.2126*f(r) + 0.7152*f(g) + 0.0722*f(b)
        hi, lo = sorted((lum(fg), lum(bg)), reverse=True)
        return (hi + 0.05) / (lo + 0.05)

    @staticmethod
    def _tokens(css, marker):
        block = css.split(marker, 1)[1].split("}", 1)[0]
        return dict(re.findall(r"(--[a-z0-9-]+):\s*(#[0-9A-Fa-f]{6});", block))

    @pytest.mark.parametrize("theme", ['light', 'dark'])
    @pytest.mark.parametrize("fg,bg", [
        ("--ink", "--ground"),
        ("--ink-2", "--ground"),
        ("--muted", "--ground"),
        ("--accent", "--ground"),
        ("--accent-2", "--ground"),
        ("--on-accent", "--accent"),
    ])
    def test_text_meets_aa(self, css, theme, fg, bg):
        t = self._tokens(css, f':root[data-theme="{theme}"] {{')
        ratio = self._ratio(t[fg], t[bg])
        assert ratio >= 4.5, f"{theme}: {fg} on {bg} is only {ratio:.2f}:1"
