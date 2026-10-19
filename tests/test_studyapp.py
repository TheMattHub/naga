"""The in-chat app template (nagasim.studyapp) and the hooks it needs in the page."""
import re

from nagasim import study, studypage, studyapp


def test_template_is_static_and_self_contained():
    html = studyapp.HTML
    assert studyapp.URI == "ui://naga/study.html"
    assert "http://" not in html and "https://" not in html and "src=" not in html
    assert html.count("<script") == 1 and html.count("</script>") == 1
    html.encode("ascii")


def test_template_calls_only_the_three_tools():
    names = set(re.findall(r'callTool\("(\w+)"', studyapp.HTML))
    assert names == {"study_page_html", "study_revert", "study_accept"}
    assert studyapp.HTML.count('"tools/call"') == 1


def test_template_has_expand_and_collapse_all():
    html = studyapp.HTML
    assert '"Expand all"' in html and '"Collapse all"' in html
    assert "d.open = p[1]" in html          # both buttons toggle every panel


def test_template_remembers_open_panels_in_memory_only():
    html = studyapp.HTML
    assert 'addEventListener("toggle"' in html and "restorePanels();" in html
    assert "localStorage" not in html and "sessionStorage" not in html


def test_template_never_uses_innerhtml():
    assert "innerHTML" not in studyapp.HTML and "document.write" not in studyapp.HTML


def test_page_marks_versions_and_actions(tmp_path):
    folder = tmp_path / "s"
    st = study.create(folder, 'Why "<b>"?')
    html = studypage.render(folder, st, study_id='a"b/c')
    assert 'id="actions"' in html
    assert 'data-study="a&quot;b/c"' in html
    assert "data-study" not in studypage.render(folder, st)
