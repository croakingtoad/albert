"""Unit tests. No network: every test builds its own fixture mirror in memory.

Run with `python -m unittest discover -s tests` from the vacode directory.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vacode import citations, db, harvest, mcp_server, normalize, search  # noqa: E402


class TestCitations(unittest.TestCase):
    def test_code_forms_normalize_to_one_key(self):
        for text in ["18.2-51", "§ 18.2-51", "§§ 18.2-51", "Section 18.2-51", "sec. 18.2-51",
                     "18.2‑51", " 18.2-51 "]:
            self.assertEqual(citations.normalize_key(text), "18.2-51", text)

    def test_decimal_and_lettered_titles(self):
        self.assertEqual(citations.normalize_key("8.01-581.1"), "8.01-581.1")
        self.assertEqual(citations.normalize_key("8.1A-101"), "8.1a-101")

    def test_administrative_code(self):
        for text in ["1VAC20-10-10", "1 VAC 20-10-10", "1vac20-10-10"]:
            self.assertEqual(citations.normalize_key(text), "1vac20-10-10", text)

    def test_constitution_roman_and_arabic(self):
        self.assertEqual(citations.normalize_key("Va. Const. art. I, § 8"), "const-art1-8")
        self.assertEqual(citations.normalize_key("Virginia Constitution article 1, section 8"),
                         "const-art1-8")
        self.assertEqual(citations.normalize_key("Va. Const. art. IV"), "const-art4")

    def test_prose_is_not_a_citation(self):
        for text in ["assault and battery", "", "what is the penalty for reckless driving", "18.2"]:
            self.assertIsNone(citations.normalize_key(text), text)

    def test_references_come_from_anchors(self):
        html = "<p>See § <a href='/vacode/2.2-4000/'>2.2-4000</a> and <a href='/vacode/18.2-51/'>18.2-51</a>.</p>"
        self.assertEqual(citations.references_in_html(html), ["2.2-4000", "18.2-51"])


class TestNormalize(unittest.TestCase):
    BODY = (
        "<p>If any person maliciously shoot, stab, cut, or wound any person, he shall be "
        "guilty of a Class 3 felony.</p>"
        "<p>Code 1950, § 18.1-65; 1960, c. 358; 1975, cc. 14, 15.</p>"
        "<p class='sidenote'>The chapters of the acts of assembly referenced in the historical "
        "citation at the end of this section may not constitute a comprehensive list.</p>"
    )

    def test_history_is_split_out_and_sidenote_dropped(self):
        parsed = normalize.parse_body(self.BODY, "Shooting, stabbing")
        self.assertIn("Class 3 felony", parsed["text"])
        self.assertNotIn("comprehensive list", parsed["text"])
        self.assertNotIn("Code 1950", parsed["text"])
        self.assertTrue(parsed["history"].startswith("Code 1950"))
        self.assertEqual(parsed["status"], "active")

    def test_repealed_and_expired_are_detected(self):
        self.assertEqual(normalize.parse_body("<p>Repealed by Acts 1981, c. 397.</p>", "Repealed")["status"],
                         "repealed")
        self.assertEqual(normalize.parse_body("<p>Expired.</p>", "Expired")["status"], "expired")

    def test_hash_is_stable_and_content_sensitive(self):
        first = normalize.parse_body(self.BODY)["hash"]
        self.assertEqual(first, normalize.parse_body(self.BODY)["hash"])
        self.assertNotEqual(first, normalize.parse_body(self.BODY + "<p>More.</p>")["hash"])

    def test_entities_and_paragraph_breaks(self):
        parsed = normalize.parse_body("<p>&quot;Person&quot; means a human.</p><p>Second.</p>")
        self.assertEqual(parsed["text"], '"Person" means a human.\n\nSecond.')


class TestSortKey(unittest.TestCase):
    def test_numbering_sorts_the_way_a_lawyer_reads_it(self):
        titles = ["10.1", "2.2", "18.2", "8.2A", "8.2", "1"]
        self.assertEqual(sorted(titles, key=db.sort_key), ["1", "2.2", "8.2", "8.2A", "10.1", "18.2"])

    def test_sections_within_a_title(self):
        sections = ["18.2-51.2", "18.2-9", "18.2-51", "18.2-100"]
        self.assertEqual(sorted(sections, key=db.sort_key),
                         ["18.2-9", "18.2-51", "18.2-51.2", "18.2-100"])


def _fixture(path=":memory:"):
    """A three-section mirror: one active, one repealed, one constitutional."""
    connection = db.connect(path)
    rows = [
        ("vacode", "18.2-51", "18.2-51", "Shooting, stabbing, etc., with intent to maim",
         "If any person maliciously shoot, stab, cut, or wound any person, he shall be guilty "
         "of a Class 3 felony.", "active", "18.2", "Crimes and Offenses Generally", "4",
         "Crimes Against the Person", "<p>See <a href='/vacode/18.2-52/'>18.2-52</a>.</p>"),
        ("vacode", "18.2-52", "18.2-52", "Malicious bodily injury by means of any caustic substance",
         "If any person maliciously causes bodily injury by any caustic substance, he is guilty "
         "of a Class 3 felony.", "active", "18.2", "Crimes and Offenses Generally", "4",
         "Crimes Against the Person", ""),
        ("vacode", "18.2-64", "18.2-64", "Repealed", "Repealed by Acts 1981, c. 397.", "repealed",
         "18.2", "Crimes and Offenses Generally", "4", "Crimes Against the Person", ""),
        ("constitution", "Va. Const. art. 1, § 8", "const-art1-8", "Criminal prosecutions",
         "That in criminal prosecutions a man hath a right to demand the cause and nature of "
         "his accusation.", "active", "1", "Bill of Rights", "8", "Criminal prosecutions", ""),
    ]
    for (corpus, citation, key, heading, text, status, title_no, title_name,
         chapter_no, chapter_name, html) in rows:
        connection.execute(
            """INSERT INTO sections (corpus, citation, citation_key, heading, body_text, body_html,
                                     status, title_number, title_name, chapter_number, chapter_name,
                                     url, sort_key, retrieved_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-01-01T00:00:00+00:00')""",
            (corpus, citation, key, heading, text, html, status, title_no, title_name,
             chapter_no, chapter_name, citations.code_url(citation),
             db.sort_key(title_no, chapter_no, citation)),
        )
    connection.execute(
        "INSERT INTO containers (corpus, kind, key, number, name, parent_key, sort_key) "
        "VALUES ('vacode', 'title', '18.2', '18.2', 'Crimes and Offenses Generally', '', ?)",
        (db.sort_key("18.2"),),
    )
    connection.execute(
        "INSERT INTO containers (corpus, kind, key, number, name, parent_key, sort_key) "
        "VALUES ('vacode', 'chapter', '18.2/4', '4', 'Crimes Against the Person', '18.2', ?)",
        (db.sort_key("18.2", "4"),),
    )
    connection.commit()
    # Deriving container keys and the reference graph the same way a real harvest does
    # keeps the fixture honest: a bug in that derivation fails these tests too.
    harvest.reindex(connection)
    return connection


class TestContainerKeys(unittest.TestCase):
    def test_each_corpus_nests_differently(self):
        self.assertEqual(harvest.container_key_for(
            "vacode", {"title_number": "18.2", "chapter_number": "4"}), "18.2/4")
        self.assertEqual(harvest.container_key_for(
            "vacode", {"title_number": "8.2", "chapter_number": ""}), "8.2")
        self.assertEqual(harvest.container_key_for(
            "admincode", {"title_number": "1", "agency_number": "20", "chapter_number": "10"}),
            "1/20/10")
        self.assertEqual(harvest.container_key_for(
            "constitution", {"title_number": "1", "chapter_number": "8"}), "1")


class TestSearch(unittest.TestCase):
    def setUp(self):
        self.connection = _fixture()

    def test_get_by_any_citation_form(self):
        for text in ["18.2-51", "§ 18.2-51", "Section 18.2-51"]:
            record = search.get(self.connection, text)
            self.assertIsNotNone(record, text)
            self.assertEqual(record["citation"], "18.2-51")
        self.assertIsNone(search.get(self.connection, "99.9-9999"))

    def test_get_extracts_cross_references(self):
        self.assertEqual(search.get(self.connection, "18.2-51")["references"], ["18.2-52"])

    def test_search_finds_text(self):
        results = search.search(self.connection, "caustic substance")
        self.assertEqual(results[0]["citation"], "18.2-52")
        self.assertEqual(results[0]["match"], "text")
        self.assertIn("caustic", results[0]["snippet"].lower())

    def test_citation_query_short_circuits_to_lookup(self):
        results = search.search(self.connection, "18.2-51")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["match"], "citation")

    def test_repealed_sections_are_hidden_by_default(self):
        self.assertEqual(search.search(self.connection, "Repealed"), [])
        included = search.search(self.connection, "Repealed", status=None)
        self.assertTrue(any(r["citation"] == "18.2-64" for r in included))

    def test_heading_outranks_body(self):
        # 'caustic' appears in 18.2-52's heading and in nothing else's, so it must lead.
        results = search.search(self.connection, "caustic")
        self.assertEqual(results[0]["citation"], "18.2-52")

    def test_corpus_and_title_filters(self):
        self.assertEqual(search.search(self.connection, "criminal", corpus="constitution")[0]["corpus"],
                         "constitution")
        self.assertEqual(search.search(self.connection, "criminal", corpus="vacode", title="99"), [])

    def test_quotes_and_operators_in_a_query_are_data_not_syntax(self):
        for query in ['felony OR "', "felony AND NOT", "felony*(", 'he said "felony"']:
            search.search(self.connection, query)  # must not raise

    def test_toc_walks_one_level_at_a_time(self):
        titles = search.toc(self.connection, "vacode")
        self.assertEqual(titles["level"], "titles")
        self.assertEqual(titles["items"][0]["number"], "18.2")
        self.assertEqual(titles["items"][0]["sections"], 3)
        chapters = search.toc(self.connection, "vacode", "18.2")
        self.assertEqual(chapters["level"], "chapters")
        self.assertEqual([c["number"] for c in chapters["items"]], ["4"])
        sections = search.toc(self.connection, "vacode", "18.2", "4")
        self.assertEqual([s["citation"] for s in sections["items"]],
                         ["18.2-51", "18.2-52", "18.2-64"])

    def test_toc_matches_a_title_exactly_not_by_prefix(self):
        # Title '1' must not pick up the chapters of 10.1, 18.2 and every other title
        # whose number happens to start with a 1.
        self.connection.execute(
            "INSERT INTO containers (corpus, kind, key, number, name, parent_key, sort_key) "
            "VALUES ('vacode', 'title', '1', '1', 'General Provisions', '', '1')")
        self.connection.commit()
        self.assertEqual(search.toc(self.connection, "vacode", "1")["items"], [])

    def test_toc_of_an_unknown_path_is_empty_not_an_error(self):
        listing = search.toc(self.connection, "vacode", "99.9", "1")
        self.assertEqual(listing["items"], [])

    def test_neighbors_and_cited_by(self):
        around = search.neighbors(self.connection, "18.2-52", span=1)
        self.assertEqual([r["citation"] for r in around], ["18.2-51", "18.2-64"])
        self.assertEqual([r["citation"] for r in search.cited_by(self.connection, "18.2-52")],
                         ["18.2-51"])
        self.assertEqual(search.neighbors(self.connection, "99.9-9999"), [])
        self.assertEqual(search.cited_by(self.connection, "99.9-9999"), [])

    def test_reindex_is_idempotent(self):
        first = harvest.reindex(self.connection)
        second = harvest.reindex(self.connection)
        self.assertEqual(first, second)
        self.assertEqual([r["citation"] for r in search.cited_by(self.connection, "18.2-52")],
                         ["18.2-51"])


class TestMcpServer(unittest.TestCase):
    def setUp(self):
        self.connection = _fixture()

    def _call(self, method, params=None, request_id=1):
        return mcp_server.handle(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
            self.connection,
        )

    def test_initialize_echoes_a_known_protocol_version(self):
        response = self._call("initialize", {"protocolVersion": "2024-11-05"})
        self.assertEqual(response["result"]["protocolVersion"], "2024-11-05")
        response = self._call("initialize", {"protocolVersion": "1999-01-01"})
        self.assertEqual(response["result"]["protocolVersion"], mcp_server.DEFAULT_PROTOCOL_VERSION)

    def test_notifications_get_no_response(self):
        self.assertIsNone(mcp_server.handle(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}, self.connection))

    def test_tools_list_is_well_formed(self):
        tools = self._call("tools/list")["result"]["tools"]
        self.assertTrue(tools)
        for tool in tools:
            self.assertIn("name", tool)
            self.assertIn("description", tool)
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_browse_accepts_a_path(self):
        response = self._call("tools/call", {"name": "browse_virginia_law",
                                             "arguments": {"path": ["18.2", "4"]}})
        text = response["result"]["content"][0]["text"]
        self.assertIn("18.2-51", text)
        self.assertIn("Chapter 4", text)

    def test_browse_also_accepts_title_and_chapter(self):
        response = self._call("tools/call", {"name": "browse_virginia_law",
                                             "arguments": {"title": "18.2", "chapter": "4"}})
        self.assertIn("18.2-51", response["result"]["content"][0]["text"])

    def test_tool_call_returns_text_content(self):
        response = self._call("tools/call", {"name": "get_virginia_law_section",
                                             "arguments": {"citation": "18.2-51"}})
        self.assertFalse(response["result"]["isError"])
        text = response["result"]["content"][0]["text"]
        self.assertIn("18.2-51", text)
        self.assertIn("law.lis.virginia.gov", text)
        self.assertIn("retrieved", text)

    def test_missing_section_explains_itself(self):
        response = self._call("tools/call", {"name": "get_virginia_law_section",
                                             "arguments": {"citation": "99.9-9999"}})
        self.assertIn("search_virginia_law", response["result"]["content"][0]["text"])

    def test_repealed_section_is_labelled(self):
        response = self._call("tools/call", {"name": "get_virginia_law_section",
                                             "arguments": {"citation": "18.2-64"}})
        self.assertIn("not current law", response["result"]["content"][0]["text"])

    def test_unknown_tool_is_a_protocol_error(self):
        self.assertEqual(self._call("tools/call", {"name": "nope"})["error"]["code"], -32602)

    def test_unknown_method_is_a_protocol_error(self):
        self.assertEqual(self._call("resources/read")["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
