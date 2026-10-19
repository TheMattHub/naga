"""The in-chat app template (nagasim.studyapp) and the hooks it needs in the page."""
import re

from nagasim import study, studypage, studyapp


def test_template_is_static_and_self_contained():
    html = studyapp.HTML
    assert studyapp.URI == "ui://naga/study.html"
    assert "http://" not in html and "https://" not in html and "src=" not in html
    assert html.count("<script") == 1 and html.count("</script>") == 1
    html.encode("ascii")


def test_template_calls_only_study_tools():
    from nagasim import agent_tools
    for html in (studyapp.HTML, studyapp.room_html()):
        names = set(re.findall(r'callTool\("(\w+)"', html))
        assert names and all(n in agent_tools.TOOLS and n.startswith("study_") for n in names), names
    assert studyapp.HTML.count('"tools/call"') == 1


def test_room_variant_uses_fetch_and_polls():
    html = studyapp.room_html()
    assert "/api/rev" in html and "/api/tool" in html and "2000" in html
    assert "parent.postMessage" not in html and "innerHTML" not in html
    html.encode("ascii")


def test_host_variant_has_clipboard_fallback():
    html = studyapp.HTML
    assert '"ui/message"' in html and "navigator.clipboard" in html and "/api/" not in html


def test_buttons_are_drawn_from_the_marks():
    html = studyapp.HTML
    for mark in ("data-latest", "data-plan-pending", "data-accepted", "data-deliverable", "data-path",
                 "#sensitivity[data-empty]"):
        assert mark in html
    for label in ("This answers my request", "Not quite", "Next iteration", "Approve plan", "Show flowchart",
                  "Make it", "Run sensitivity"):
        assert f'"{label}"' in html


def test_every_study_tool_carries_the_ui():
    from nagasim import agent_tools, mcp_server
    assert {n for n in agent_tools.TOOLS if n.startswith("study_")} == set(mcp_server._STUDY_UI)
    assert mcp_server._STUDY_UI["study_page_html"]["visibility"] == ["app"]


def test_template_has_expand_and_collapse_all():
    html = studyapp.HTML
    assert '"Expand all"' in html and '"Collapse all"' in html
    assert "d.open = p[1]" in html          # both buttons toggle every panel


def test_template_remembers_open_panels_in_memory_only():
    html = studyapp.HTML
    assert 'addEventListener("toggle"' in html and "restorePanels();" in html
    assert "localStorage" not in html and "sessionStorage" not in html


def test_template_never_uses_innerhtml():
    for html in (studyapp.HTML, studyapp.room_html()):
        assert "innerHTML" not in html and "document.write" not in html


def test_page_marks_versions_and_actions(tmp_path):
    folder = tmp_path / "s"
    st = study.create(folder, 'Why "<b>"?')
    html = studypage.render(folder, st, study_id='a"b/c')
    assert 'id="actions"' in html
    assert 'data-study="a&quot;b/c"' in html
    assert "data-study" not in studypage.render(folder, st)


def test_room_puts_the_assistant_in_a_side_column_on_wide_screens():
    assert "@media (min-width:1100px)" in studyapp.room_html()
    assert "@media" not in studyapp.HTML


def test_insert_drops_javascript_urls():
    html = studyapp.room_html()
    assert 'indexOf("javascript:") === 0' in html and r'replace(/\s+/g, "")' in html and "toLowerCase()" in html
