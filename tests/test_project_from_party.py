#!/usr/bin/env python3
"""Party → Project: creating a project *from* the Parties module.

The integration used to be one-way: a Project's Client name auto-created a
Party (``PROJECTS_PARTIES_INTEGRATION.md``).  The reverse is now first class:

* every party row on ``/hdc/parties`` carries a **New Project** action that
  opens the ordinary Add Project form with the client + phone filled;
* the party ledger offers the same action (and, for a client with no project
  yet, a card explaining it);
* saving that form links the project to the exact ``CashFlowParty`` row
  (``Project.client_party_id``), not merely to a name;
* a typed new client still behaves exactly like the project-first flow — the
  party is created as *Client / Owner* and filed under Clients / Owners;
* the directory sync backfills the direct link for legacy projects, and the
  Projects list / detail link back to the party ledger.

Run with:
    python -m unittest tests.test_project_from_party -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

try:  # works both as ``tests.test_project_from_party`` and from tests/
    from tests.qa_support import IsolatedAppTest
except ImportError:  # pragma: no cover - direct run from the tests directory
    from qa_support import IsolatedAppTest

from sqlalchemy import func                                        # noqa: E402

from hdc.extensions import db                                     # noqa: E402
from hdc.models.cashflow import CashFlowParty                     # noqa: E402
from hdc.models.projects import Project                           # noqa: E402
from hdc.services.cashflow_register import ensure_party           # noqa: E402
from hdc.services.parties import sync_project_clients_as_parties  # noqa: E402

PARTIES_URL = '/hdc/parties'
ADD_PROJECT_URL = '/hdc/projects/add'


class ProjectFromPartyTestCase(IsolatedAppTest):
    """The Add Project form is reachable from a party, and stays linked."""

    # ── helpers ─────────────────────────────────────────────────────────────
    def _party(self, name='Owner One', party_type='client', phone='0300-1234567'):
        with self.app.app_context():
            party, _created = ensure_party(name, party_type=party_type, phone=phone)
            db.session.commit()
            return int(party.id)

    def _create_project_from_party(self, party_id, *, code='HDC-T1', name='Villa One',
                                   client='Owner One', phone='0300-1234567'):
        self.form(ADD_PROJECT_URL, project_code=code, name=name, client=client,
                  client_phone=phone, party_id=str(party_id), location='DHA',
                  start_date='2026-01-01', status='active')

    @staticmethod
    def _party_for(project_code):
        return Project.query.filter_by(project_code=project_code).one()

    # ── the form is opened with the party already chosen ────────────────────
    def test_add_project_page_prefills_name_phone_and_hidden_party_link(self):
        party_id = self._party()
        html = self.client.get(f'{ADD_PROJECT_URL}?party_id={party_id}').get_data(as_text=True)

        self.assertIn('Starting from a Party', html)
        self.assertIn('value="Owner One"', html)
        self.assertIn('value="0300-1234567"', html)
        self.assertIn(f'name="party_id" value="{party_id}"', html)
        self.assertIn(f'/hdc/parties/{party_id}', html)

    def test_client_name_link_still_prefills_the_party(self):
        """The older ``?client=<name>`` link keeps working (and finds phone)."""
        party_id = self._party('Name Only Owner', phone='0322-0000000')
        html = self.client.get(
            f'{ADD_PROJECT_URL}?client=Name%20Only%20Owner').get_data(as_text=True)
        self.assertIn('value="Name Only Owner"', html)
        self.assertIn('value="0322-0000000"', html)
        self.assertIn(f'name="party_id" value="{party_id}"', html)

    # ── saving links the exact party row ────────────────────────────────────
    def test_project_created_from_party_points_at_that_party(self):
        party_id = self._party()
        self._create_project_from_party(party_id)

        with self.app.app_context():
            project = self._party_for('HDC-T1')
            self.assertEqual(int(project.client_party_id), party_id)
            self.assertEqual(project.client, 'Owner One')
            # The same party is reused — no duplicate row with the same name.
            matches = (CashFlowParty.query
                       .filter(func.lower(func.trim(CashFlowParty.name)) == 'owner one')
                       .all())
            self.assertEqual(len(matches), 1)

    def test_slightly_different_casing_keeps_the_same_party(self):
        party_id = self._party('Owner One')
        self._create_project_from_party(party_id, client='  owner   one  ')

        with self.app.app_context():
            project = self._party_for('HDC-T1')
            self.assertEqual(int(project.client_party_id), party_id)
            # The party's own spelling wins for the project's display name.
            self.assertEqual(project.client, 'Owner One')

    def test_typed_new_client_still_creates_client_party_like_before(self):
        """The project-first behaviour is unchanged when no party was picked."""
        self.form(ADD_PROJECT_URL, project_code='HDC-T9', name='Typed Site',
                  client='Brand New Owner', client_phone='0333-1234567',
                  location='Gulberg', start_date='2026-02-01', status='active')

        with self.app.app_context():
            project = self._party_for('HDC-T9')
            party = (CashFlowParty.query
                     .filter(func.lower(func.trim(CashFlowParty.name)) == 'brand new owner')
                     .one())
            self.assertEqual(party.party_type, 'client')
            self.assertEqual(int(project.client_party_id), int(party.id))
            self.assertEqual(project.client, 'Brand New Owner')

            from hdc.services.party_categories import effective_party_category
            category = effective_party_category(party)
            self.assertIsNotNone(category)
            self.assertEqual(category.key, 'client')

    # ── both pages offer the action ─────────────────────────────────────────
    def test_parties_directory_offers_new_project_for_each_party(self):
        party_id = self._party()
        html = self.client.get(PARTIES_URL).get_data(as_text=True)
        self.assertIn(f'/hdc/projects/add?party_id={party_id}', html)
        self.assertIn('Create a project for Owner One', html)

    def test_party_ledger_offers_new_project_and_empty_state(self):
        party_id = self._party()
        html = self.client.get(f'{PARTIES_URL}/{party_id}').get_data(as_text=True)
        self.assertIn(f'/hdc/projects/add?party_id={party_id}', html)
        self.assertIn('Create the first project', html)

    def test_party_ledger_shows_the_project_created_from_it(self):
        party_id = self._party()
        self._create_project_from_party(party_id)

        html = self.client.get(f'{PARTIES_URL}/{party_id}').get_data(as_text=True)
        self.assertIn('Projects owned by Owner One', html)
        self.assertIn('Villa One', html)
        self.assertNotIn('Create the first project', html)

    def test_project_detail_links_back_to_the_party_ledger(self):
        party_id = self._party()
        self._create_project_from_party(party_id)

        with self.app.app_context():
            project_id = int(self._party_for('HDC-T1').id)
        html = self.client.get(f'/hdc/projects/{project_id}').get_data(as_text=True)
        self.assertIn(f'/hdc/parties/{party_id}', html)
        self.assertIn('Open Party Ledger (Owner One)', html)

    def test_projects_list_links_the_client_to_the_party(self):
        party_id = self._party()
        self._create_project_from_party(party_id)

        html = self.client.get('/hdc/projects').get_data(as_text=True)
        self.assertIn(f'/hdc/parties/{party_id}', html)

    # ── legacy data + edits ─────────────────────────────────────────────────
    def test_sync_backfills_the_direct_link_for_legacy_projects(self):
        with self.app.app_context():
            party, _ = ensure_party('Legacy Owner', party_type='client')
            db.session.add(Project(name='Legacy Site', project_code='HDC-L1',
                                   client='Legacy Owner'))
            db.session.commit()
            party_id = int(party.id)

            created, reactivated, total, linked = sync_project_clients_as_parties()
            db.session.commit()

            self.assertGreaterEqual(linked, 1)
            project = self._party_for('HDC-L1')
            self.assertEqual(int(project.client_party_id), party_id)

    def test_fk_link_survives_a_client_spelling_edit(self):
        """The party's projects follow the direct link, not just the text."""
        party_id = self._party('Owner One')
        with self.app.app_context():
            db.session.add(Project(name='Drifted Site', project_code='HDC-D1',
                                   client='Owner One (old spelling)',
                                   client_party_id=party_id))
            db.session.commit()

        html = self.client.get(f'{PARTIES_URL}/{party_id}').get_data(as_text=True)
        self.assertIn('Projects owned by Owner One', html)
        self.assertIn('Drifted Site', html)

    def test_editing_the_client_repoints_the_project(self):
        party_id = self._party('Old Owner')
        self._create_project_from_party(party_id, code='HDC-E1', name='Edit Site',
                                        client='Old Owner')

        with self.app.app_context():
            project_id = int(self._party_for('HDC-E1').id)

        self.form(f'/hdc/projects/{project_id}/edit', name='Edit Site',
                  client='New Owner', client_phone='0344-5555555',
                  location='DHA', status='active')

        with self.app.app_context():
            project = db.session.get(Project, project_id)
            new_party = (CashFlowParty.query
                         .filter(func.lower(func.trim(CashFlowParty.name)) == 'new owner')
                         .one())
            self.assertEqual(int(project.client_party_id), int(new_party.id))


if __name__ == '__main__':
    unittest.main()
