"""The observation dashboard links to the mounted warehouse application."""
from html.parser import HTMLParser
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "cloud/frontend"


class Navigation(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.links.append(dict(attrs))


def test_dashboard_navigation_keeps_full_warehouse_entry_and_mobile_label():
    navigation = Navigation()
    navigation.feed((FRONTEND / "index.html").read_text())
    warehouse = [link for link in navigation.links
                 if 'dbprog-nav' in link.get('class', '').split()]
    assert len(warehouse) == 1
    assert warehouse[0]['href'] == '/dbprog'
    css = (FRONTEND / "dbprog.css").read_text()
    assert '.dbprog-nav.coverage-link::after' in css
    assert 'content: "数仓"' in css
