.. _search:

Search customization
====================

This guide shows you different ways that you can customize your Sphinx
project's built-in search.

Overview
--------

Remove a page from search results
---------------------------------
Add the ``:no-search`` :ref:`metadata` field to the top of the file that you
want to remove from search results.

Customize the search results scorer
-----------------------------------

To use this feature, your project must :ref:`inherit the basic theme <basic>`.

.. _basic:

Appendix: basic theme inheritance
---------------------------------

.. _basic: https://github.com/sphinx-doc/sphinx/tree/master/sphinx/themes/basic

Some of the customizations mentioned in this guide are only available when the
:ref:`HTML theme <html-themes>` that your project uses inherits the `basic`_
theme. See :ref:`extension-html-theme-configuration` for more on inheritance.

An easy way to confirm that your theme inherits ``basic`` is to inspect your
HTML output and make sure that ``searchtools.js`` exists in your static assets
directory.

You can also inspect the ``theme.conf`` file in your theme's source code
repository and confirm that you see a configuration like this:

.. code-block:: text

   [theme]
   inherit = basic

If your theme inherits something else, for example ``other_theme``, check the
``theme.conf`` for ``other_theme``. ``basic`` just needs to eventually get
inherited at some point. Your theme doesn't need to directly inherit ``basic``.
