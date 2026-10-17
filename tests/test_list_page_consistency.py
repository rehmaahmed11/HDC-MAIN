#!/usr/bin/env python3
"""Every "list of names" screen is one page shape.

The category lists used to be four different pages:

* Expense Categories — a centred 840px "workspace" with a hero tile, a count
  pill and a one-line quick-add bar, sitting nowhere near its neighbours'
  edges;
* Trades — the same private workspace clone;
* Allowance Categories — a page head whose Add button used `btn-purple` /
  `text-purple`, classes no stylesheet defines, so it rendered unstyled;
* Personal Expense Categories — the title inside a card header, with the Add
  button, the Back link and the list spread across three different rows.

Every other list in the app (Projects, Workers, Materials, Suppliers…) uses
one shape: a page head that is a direct child of <main class="main-content">
with the row count in the heading and the add action as its own child, then a
full-width `card hdc-card` holding a `table hdc-table`.

That shape is now rendered by one macro,
`templates/hdc/shared/_page_head.html`, and these tests pin it: if a template
is hand-edited back into a bespoke layout, or grows a class that does not
exist in any stylesheet, the suite fails instead of the page quietly looking
different from the rest of the app.
"""
from __future__ import annotations

import os
import re
import unittest

from qa_support import IsolatedAppTest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(REPO_ROOT, 'templates', 'hdc')

#: page title -> template, for the four "list of names" screens.
LIST_PAGES = {
    'Expense Categories': os.path.join(TEMPLATES, 'expenses', 'expense_categories.html'),
    'Trades': os.path.join(TEMPLATES, 'workers', 'trades.html'),
    'Allowance Categories': os.path.join(TEMPLATES, 'office', 'allowance_categories.html'),
    'Personal Expense Categories': os.path.join(TEMPLATES, 'accounts',
                                                'personal_expense_categories.html'),
}

#: Rendered URLs for the three pages that do not need a filter argument.
RENDERED_PAGES = {
    'Expense Categories': '/hdc/expense_categories',
    'Trades': '/hdc/trades',
    'Allowance Categories': '/hdc/office-management/allowance-categories',
    'Personal Expense Categories': '/hdc/personal-management/categories',
}


def read(path):
    with open(path, encoding='utf-8') as handle:
        return handle.read()


class ListPageTemplateTest(unittest.TestCase):
    """The templates share one page head and one list shell."""

    def test_every_list_page_uses_the_shared_page_head(self):
        for title, path in LIST_PAGES.items():
            source = read(path)
            self.assertIn('{% from "shared/_page_head.html" import page_head %}',
                          source, '%s must import the shared page head' % title)
            self.assertIn('{% call page_head(', source,
                          '%s must render its heading through the macro' % title)

    def test_no_list_page_keeps_its_own_workspace_shell(self):
        retired = ('expense-category-page', 'expense-category-hero',
                   'expense-category-quick-add', 'trades-shell', 'trades-hero',
                   'trades-quick-add', 'trade-pill')
        for title, path in LIST_PAGES.items():
            source = read(path)
            for marker in retired:
                self.assertNotIn(marker, source,
                                 '%s still ships the retired %s shell' % (title, marker))

    def test_every_list_page_uses_the_standard_card_and_table(self):
        for title, path in LIST_PAGES.items():
            source = read(path)
            self.assertIn('card hdc-card', source,
                          '%s must use the standard card' % title)
            self.assertIn('table hdc-table', source,
                          '%s must use the standard table' % title)
            self.assertIn('table-responsive', source,
                          '%s must wrap its table for phones' % title)

    def test_add_action_opens_a_modal_like_the_other_lists(self):
        """Adding a row is a modal everywhere else — so it is here too."""
        for title, path in LIST_PAGES.items():
            source = read(path)
            self.assertIn('data-bs-toggle="modal"', source,
                          '%s must open its add form in a modal' % title)
            self.assertIn('data-bs-target="#', source,
                          '%s must name the modal it opens' % title)

    def test_page_head_keeps_its_actions_in_one_group(self):
        """A page may have two header controls (Personal: Back + Add).

        They belong in one group that the adaptive layer can widen as a unit;
        without it the phone kept a shrunken inline pair while every sibling
        list showed a full-width Add bar under the heading.
        """
        source = read(os.path.join(TEMPLATES, 'shared', '_page_head.html'))
        self.assertIn('hdc-page-head-actions', source,
                      'the shared page head must group its action(s)')
        css = ''
        for name in os.listdir(os.path.join(REPO_ROOT, 'static', 'hdc', 'css')):
            if name.endswith('.css'):
                css += read(os.path.join(REPO_ROOT, 'static', 'hdc', 'css', name))
        self.assertIn('.hdc-page-head-actions', css,
                      'no stylesheet sizes the shared page-head action group')

    def test_no_undefined_purple_classes(self):
        """`btn-purple` / `text-purple` were never in any stylesheet."""
        css = ''
        for name in os.listdir(os.path.join(REPO_ROOT, 'static', 'hdc', 'css')):
            if name.endswith('.css'):
                css += read(os.path.join(REPO_ROOT, 'static', 'hdc', 'css', name))
        for title, path in LIST_PAGES.items():
            source = read(path)
            # Only a real class attribute counts; the templates' own comments
            # mention the retired names on purpose.
            for cls in ('btn-purple', 'text-purple'):
                used = re.search(r'class="[^"]*\\b' + cls + r'\\b', source)
                if used:
                    self.assertIn('.' + cls, css,
                                  '%s uses %s but no stylesheet defines it'
                                  % (title, cls))


class ListPageRenderTest(IsolatedAppTest):
    """The served pages carry the shared page head and one card/table."""

    def test_every_list_page_renders_the_same_page_shape(self):
        # Give the personal list a row so it renders its table, not the
        # empty-state card (both are legitimate states of the same page).
        self.form('/hdc/personal-management/categories', action='add',
                  name='Advance', description='Salary advance')
        for title, url in RENDERED_PAGES.items():
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            html = response.get_data(as_text=True)

            # one page head, a direct child of <main>, with the title + count
            heads = re.findall(
                r'<div class="d-flex justify-content-between align-items-center '
                r'mt-2 mb-3">\s*<h5 class="mb-0">.*?' + re.escape(title),
                html, re.S)
            self.assertEqual(len(heads), 1,
                             '%s: expected exactly one shared page head' % title)
            main_index = html.index('main class="main-content')
            self.assertGreater(html.index(heads[0]), main_index,
                               '%s: the page head must live inside <main>' % title)

            # the standard list shell
            self.assertIn('card hdc-card', html, title)
            self.assertIn('table hdc-table', html, title)

            # the retired workspace chrome is gone from the rendered page
            for marker in ('expense-category-page', 'expense-category-hero',
                           'expense-category-quick-add', 'trades-shell',
                           'trades-hero', 'trades-quick-add'):
                self.assertNotIn(marker, html,
                                 '%s still renders %s' % (title, marker))

    def test_the_two_name_lists_share_the_row_rename_component(self):
        for url, action in (('/hdc/expense_categories', 'edit_category'),
                            ('/hdc/trades', 'edit_trade')):
            html = self.client.get(url).get_data(as_text=True)
            self.assertIn('data-hdc-rename-table', html, url)
            self.assertIn('/hdc_static/js/core/row_rename.js', html, url)
            self.assertIn('value="%s"' % action, html, url)
            # read-only rows: exactly one input per row, hidden until asked
            self.assertIn('hdc-rename-form d-none', html, url)
            self.assertIn('hdc-rename-view', html, url)
            self.assertIn('hdc-rename-actions', html, url)


if __name__ == '__main__':
    unittest.main()
