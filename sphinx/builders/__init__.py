"""Builder superclass for all builders."""

from __future__ import annotations

import codecs
import pickle
import re
import time
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, final

from docutils import nodes

from sphinx._cli.util.colour import bold
from sphinx.deprecation import _deprecation_warning
from sphinx.environment import (
    CONFIG_CHANGED_REASON,
    CONFIG_OK,
    _CurrentDocument,
)
from sphinx.environment.adapters.asset import ImageAdapter
from sphinx.errors import SphinxError
from sphinx.locale import __
from sphinx.util import get_filetype, logging
from sphinx.util._importer import import_object
from sphinx.util._pathlib import _StrPathProperty
from sphinx.util.build_phase import BuildPhase
from sphinx.util.display import progress_message, status_iterator
from sphinx.util.docutils import _parse_str_to_doctree
from sphinx.util.i18n import CatalogRepository, docname_to_domain
from sphinx.util.osutil import ensuredir, relative_uri, relpath
from sphinx.util.parallel import (
    ParallelTasks,
    SerialTasks,
    make_chunks,
    parallel_available,
)

# side effect: registers roles and directives
from sphinx import directives  # NoQA: F401  isort:skip
from sphinx import roles  # NoQA: F401  isort:skip

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence, Set
    from gettext import NullTranslations
    from typing import Any, ClassVar, Literal

    from docutils.nodes import Node

    from sphinx.application import Sphinx
    from sphinx.config import Config
    from sphinx.environment import (
        BuildEnvironment,
    )
    from sphinx.events import EventManager
    from sphinx.util.i18n import CatalogInfo
    from sphinx.util.tags import Tags


logger = logging.getLogger(__name__)


class Builder:
    """Builds target formats from the reST sources."""

    #: The builder's name.
    #: This is the value used to select builders on the command line.
    name: ClassVar[str] = ''
    #: The builder's output format, or '' if no document output is produced.
    #: This is commonly the file extension, e.g. "html",
    #: though any string value is accepted.
    #: The builder's format string can be used by various components
    #: such as :class:`.SphinxPostTransform` or extensions to determine
    #: their compatibility with the builder.
    format: ClassVar[str] = ''
    #: The message emitted upon successful build completion.
    #: This can be a printf-style template string
    #: with the following keys: ``outdir``, ``project``
    epilog: ClassVar[str] = ''

    #: default translator class for the builder.  This can be overridden by
    #: :py:meth:`~sphinx.application.Sphinx.set_translator`.
    default_translator_class: ClassVar[type[nodes.NodeVisitor]]
    # doctree versioning method
    versioning_method: ClassVar[str] = 'none'
    versioning_compare: ClassVar[bool] = False
    #: Whether it is safe to make parallel :meth:`~.Builder.write_doc` calls.
    allow_parallel: ClassVar[bool] = False
    # support translation
    use_message_catalog: ClassVar[bool] = True

    #: The list of MIME types of image formats supported by the builder.
    #: Image files are searched in the order in which they appear here.
    supported_image_types: ClassVar[list[str]] = []
    #: The builder can produce output documents that may fetch external images when opened.
    supported_remote_images: ClassVar[bool] = False
    #: The file format produced by the builder allows images to be embedded using data-URIs.
    supported_data_uri_images: ClassVar[bool] = False

    phase: BuildPhase = BuildPhase.INITIALIZATION

    srcdir = _StrPathProperty()
    confdir = _StrPathProperty()
    outdir = _StrPathProperty()
    doctreedir = _StrPathProperty()

    def __init__(self, app: Sphinx, env: BuildEnvironment) -> None:
        self.srcdir = app.srcdir
        self.confdir = app.confdir
        self.outdir = app.outdir
        self.doctreedir = app.doctreedir
        ensuredir(self.doctreedir)

        self._app: Sphinx = app
        self.env: BuildEnvironment = env
        self.env.set_versioning_method(self.versioning_method, self.versioning_compare)
        self.events: EventManager = app.events
        self.config: Config = app.config
        self.tags: Tags = app.tags
        self.tags.add(self.format)
        self.tags.add(self.name)
        self.tags.add(f'format_{self.format}')
        self.tags.add(f'builder_{self.name}')
        self._registry = app.registry

        # images that need to be copied over (source -> dest)
        self.images: dict[str, str] = {}
        # basename of images directory
        self.imagedir = ''
        # relative path to image directory from current docname (used at writing docs)
        self.imgpath = ''

        # these get set later
        self.parallel_ok = False
        self.finish_tasks: Any = None

    @property
    def app(self) -> Sphinx:
        cls_module = self.__class__.__module__
        cls_name = self.__class__.__qualname__
        _deprecation_warning(cls_module, f'{cls_name}.app', remove=(10, 0))
        return self._app

    @property
    def _translator(self) -> NullTranslations | None:
        return self._app.translator

    def get_translator_class(self, *args: Any) -> type[nodes.NodeVisitor]:
        """Return a class of translator."""
        return self._registry.get_translator_class(self)

    def create_translator(self, *args: Any) -> nodes.NodeVisitor:
        """Return an instance of translator.

        This method returns an instance of ``default_translator_class`` by default.
        Users can replace the translator class with ``app.set_translator()`` API.
        """
        return self._registry.create_translator(self, *args)

    # helper methods
    def init(self) -> None:
        """Load necessary templates and perform initialization.  The default
        implementation does nothing.
        """
        pass

    def create_template_bridge(self) -> None:
        """Return the template bridge configured."""
        if self.config.template_bridge:
            template_bridge_cls = import_object(
                self.config.template_bridge,
                source='template_bridge setting',
            )
            self.templates = template_bridge_cls()
        else:
            from sphinx.jinja2glue import BuiltinTemplateLoader

            self.templates = BuiltinTemplateLoader()

    def get_target_uri(self, docname: str, typ: str | None = None) -> str:
        """Return the target URI for a document name.

        *typ* can be used to qualify the link characteristic for individual
        builders.
        """
        raise NotImplementedError

    def get_relative_uri(self, from_: str, to: str, typ: str | None = None) -> str:
        """Return a relative URI between two source filenames.

        :raises: :exc:`!NoUri` if there's no way to return a sensible URI.
        """
        return relative_uri(
            self.get_target_uri(from_),
            self.get_target_uri(to, typ),
        )

    def get_outdated_docs(self) -> str | Iterable[str]:
        """Return an iterable of output files that are outdated, or a string
        describing what an update build will build.

        If the builder does not output individual files corresponding to
        source files, return a string here.  If it does, return an iterable
        of those files that need to be written.
        """
        raise NotImplementedError

    def get_asset_paths(self) -> list[str]:
        """Return list of paths for assets (ex. templates, CSS, etc.)."""
        return []

    def post_process_images(self, doctree: Node) -> None:
        """Pick the best candidate for all image URIs."""
        images = ImageAdapter(self.env)
        for node in doctree.findall(nodes.image):
            if '?' in node['candidates']:
                # don't rewrite nonlocal image URIs
                continue
            if '*' not in node['candidates']:
                for imgtype in self.supported_image_types:
                    candidate = node['candidates'].get(imgtype, None)
                    if candidate:
                        break
                else:
                    mimetypes = sorted(node['candidates'])
                    image_uri = images.get_original_image_uri(node['uri'])
                    if mimetypes:
                        logger.warning(
                            __('a suitable image for %s builder not found: %s (%s)'),
                            self.name,
                            mimetypes,
                            image_uri,
                            location=node,
                        )
                    else:
                        logger.warning(
                            __('a suitable image for %s builder not found: %s'),
                            self.name,
                            image_uri,
                            location=node,
                        )
                    continue
                node['uri'] = candidate
            else:
                candidate = node['uri']
            if candidate not in self.env.images:
                # non-existing URI; let it alone
                continue
            self.images[candidate] = self.env.images[candidate][1]

    # compile po methods

    def compile_catalogs(self, catalogs: set[CatalogInfo], message: str) -> None:
        if not self.config.gettext_auto_build:
            return

        def cat2relpath(cat: CatalogInfo, srcdir: Path = self.srcdir) -> str:
            return Path(relpath(cat.mo_path, srcdir)).as_posix()

        logger.info(bold(__('building [mo]: ')) + message)  # NoQA: G003
        for catalog in status_iterator(
            catalogs,
            __('writing output... '),
            'darkgreen',
            len(catalogs),
            self.config.verbosity,
            stringify_func=cat2relpath,
        ):
            catalog.write_mo(
                self.config.language, self.config.gettext_allow_fuzzy_translations
            )

    def compile_all_catalogs(self) -> None:
        repo = CatalogRepository(
            self.srcdir,
            self.config.locale_dirs,
            self.config.language,
            self.config.source_encoding,
        )
        message = __('all of %d po files') % len(list(repo.catalogs))
        self.compile_catalogs(set(repo.catalogs), message)

    def compile_specific_catalogs(self, specified_files: Iterable[Path]) -> None:
        env = self.env
        gettext_compact = self.config.gettext_compact

        domains = {
            docname_to_domain(docname, gettext_compact) if docname else None
            for file in specified_files
            if (docname := env.path2doc(file))
        }
        catalogs = set()
        repo = CatalogRepository(
            self.srcdir,
            self.config.locale_dirs,
            self.config.language,
            self.config.source_encoding,
        )
        for catalog in repo.catalogs:
            if catalog.domain in domains and catalog.is_outdated():
                catalogs.add(catalog)
        message = __('targets for %d po files that are specified') % len(catalogs)
        self.compile_catalogs(catalogs, message)

    # TODO(stephenfin): This would make more sense as 'compile_outdated_catalogs'
    def compile_update_catalogs(self) -> None:
        repo = CatalogRepository(
            self.srcdir,
            self.config.locale_dirs,
            self.config.language,
            self.config.source_encoding,
        )
        catalogs = {c for c in repo.catalogs if c.is_outdated()}
        message = __('targets for %d po files that are out of date') % len(catalogs)
        self.compile_catalogs(catalogs, message)

    # build methods

    @final
    def build_all(self) -> None:
        """Build all source files."""
        self.compile_all_catalogs()

        self.build(None, summary=__('all source files'), method='all')

    @final
    def build_specific(self, filenames: Sequence[Path]) -> None:
        """Only rebuild as much as needed for changes in the *filenames*."""
        docnames: list[str] = []

        filenames = [Path(filename).resolve() for filename in filenames]
        for filename in filenames:
            if not filename.is_file():
                logger.warning(
                    __('file %r given on command line does not exist, '), filename
                )
                continue

            if not filename.is_relative_to(self.srcdir):
                logger.warning(
                    __(
                        'file %r given on command line is not under the '
                        'source directory, ignoring'
                    ),
                    filename,
                )
                continue

            docname = self.env.path2doc(filename)
            if not docname:
                logger.warning(
                    __(
                        'file %r given on command line is not a valid '
                        'document, ignoring'
                    ),
                    filename,
                )
                continue

            docnames.append(docname)

        self.compile_specific_catalogs(filenames)

        self.build(
            docnames,
            summary=__('%d source files given on command line') % len(docnames),
            method='specific',
        )

    @final
    def build_update(self) -> None:
        """Only rebuild what was changed or added since last build."""
        self.compile_update_catalogs()

        to_build = self.get_outdated_docs()
        if isinstance(to_build, str):
            self.build(['__all__'], summary=to_build, method='update')
        else:
            to_build = set(to_build)
            self.build(
                to_build,
                summary=__('targets for %d source files that are out of date')
                % len(to_build),
                method='update',
            )

    @final
    def build(
        self,
        docnames: Iterable[str] | None,
        summary: str | None = None,
        method: Literal['all', 'specific', 'update'] = 'update',
    ) -> None:
        """Main build method, usually called by a specific ``build_*`` method.

        First updates the environment, and then calls
        :meth:`!write`.
        """
        if summary:
            logger.info(bold(__('building [%s]: ')) + summary, self.name)  # NoQA: G003

        # while reading, collect all warnings from docutils
        with (
            nullcontext()
            if self._app._exception_on_warning
            else logging.pending_warnings()
        ):
            updated_docnames = set(self.read())

        doccount = len(updated_docnames)
        logger.info(bold(__('looking for now-outdated files... ')), nonl=True)
        updated_docnames.update(self.env.check_dependents(self._app, updated_docnames))
        outdated = len(updated_docnames) - doccount
        if outdated:
            logger.info(__('%d found'), outdated)
        else:
            logger.info(__('none found'))

        if updated_docnames:
            # save the environment
            from sphinx.application import ENV_PICKLE_FILENAME

            with (
                progress_message(__('pickling environment')),
                open(self.doctreedir / ENV_PICKLE_FILENAME, 'wb') as f,
            ):
                pickle.dump(self.env, f, pickle.HIGHEST_PROTOCOL)

            # global actions
            self.phase = BuildPhase.CONSISTENCY_CHECK
            with progress_message(__('checking consistency')):
                self.env.check_consistency()
        else:
            if method == 'update' and not docnames:
                logger.info(bold(__('no targets are out of date.')))

        self.phase = BuildPhase.RESOLVING

        # filter "docnames" (list of outdated files) by the updated
        # found_docs of the environment; this will remove docs that
        # have since been removed
        if docnames and docnames != ['__all__']:
            docnames = set(docnames) & self.env.found_docs

        # determine if we can write in parallel
        if parallel_available and self._app.parallel > 1 and self.allow_parallel:
            self.parallel_ok = self._app.is_parallel_allowed('write')
        else:
            self.parallel_ok = False

        #  create a task executor to use for misc. "finish-up" tasks
        # if self.parallel_ok:
        #     self.finish_tasks = ParallelTasks(self._app.parallel)
        # else:
        # for now, just execute them serially
        self.finish_tasks = SerialTasks()

        # write all "normal" documents (or everything for some builders)
        self.write(docnames, updated_docnames, method)

        # finish (write static files etc.)
        self.finish()

        # wait for all tasks
        self.finish_tasks.join()

    @final
    def read(self) -> list[str]:
        """(Re-)read all files new or changed since last update.

        Store all environment docnames in the canonical format (ie using SEP as
        a separator in place of os.path.sep).
        """
        logger.info(bold(__('updating environment: ')), nonl=True)

        self.env.find_files(self.config, self)
        updated = self.env.config_status != CONFIG_OK
        added, changed, removed = self.env.get_outdated_files(updated)

        # allow user intervention as well
        for docs in self.events.emit(
            'env-get-outdated', self.env, added, changed, removed
        ):
            changed.update(set(docs) & self.env.found_docs)

        # if files were added or removed, all documents with globbed toctrees
        # must be reread
        if added or removed:
            # ... but not those that already were removed
            changed.update(self.env.glob_toctrees & self.env.found_docs)

        if updated:  # explain the change iff build config status was not ok
            reason = CONFIG_CHANGED_REASON.get(self.env.config_status, '') + (
                self.env.config_status_extra or ''
            )
            logger.info('[%s] ', reason, nonl=True)

        logger.info(
            __('%s added, %s changed, %s removed'),
            len(added),
            len(changed),
            len(removed),
        )

        # clear all files no longer present
        for docname in removed:
            self.events.emit('env-purge-doc', self.env, docname)
            self.env.clear_doc(docname)

        # read all new and changed files
        docnames = sorted(added | changed)
        # allow changing and reordering the list of docs to read
        self.events.emit('env-before-read-docs', self.env, docnames)

        # check if we should do parallel or serial read
        if parallel_available and self._app.parallel > 1:
            par_ok = self._app.is_parallel_allowed('read')
        else:
            par_ok = False

        if par_ok:
            self._read_parallel(docnames, nproc=self._app.parallel)
        else:
            self._read_serial(docnames)

        if self.config.master_doc not in self.env.all_docs:
            from sphinx.project import EXCLUDE_PATHS
            from sphinx.util.matching import _translate_pattern

            master_doc_path = self.env.doc2path(self.config.master_doc)
            master_doc_canon = master_doc_path.as_posix()
            for pat in EXCLUDE_PATHS:
                if not re.match(_translate_pattern(pat), master_doc_canon):
                    continue
                msg = __(
                    'Sphinx is unable to load the master document (%s) '
                    'because it matches a built-in exclude pattern %r. '
                    'Please move your master document to a different location.'
                )
                raise SphinxError(msg % (master_doc_path, pat))
            for pat in self.config.exclude_patterns:
                if not re.match(_translate_pattern(pat), master_doc_canon):
                    continue
                msg = __(
                    'Sphinx is unable to load the master document (%s) '
                    'because it matches an exclude pattern specified '
                    'in conf.py, %r. '
                    'Please remove this pattern from conf.py.'
                )
                raise SphinxError(msg % (master_doc_path, pat))
            if set(self.config.include_patterns) != {'**'} and not any(
                re.match(_translate_pattern(pat), master_doc_canon)
                for pat in self.config.include_patterns
            ):
                msg = __(
                    'Sphinx is unable to load the master document (%s) '
                    'because it is not included in the custom include_patterns = %r. '
                    'Ensure that a pattern in include_patterns matches the '
                    'master document.'
                )
                raise SphinxError(msg % (master_doc_path, self.config.include_patterns))
            msg = __(
                'Sphinx is unable to load the master document (%s). '
                'The master document must be within the source directory '
                'or a subdirectory of it.'
            )
            raise SphinxError(msg % master_doc_path)

        for retval in self.events.emit('env-updated', self.env):
            if retval is not None:
                docnames.extend(retval)

        # workaround: marked as okay to call builder.read() twice in same process
        self.env.config_status = CONFIG_OK

        return sorted(docnames)

    def _read_serial(self, docnames: list[str]) -> None:
        for docname in status_iterator(
            docnames,
            __('reading sources... '),
            'purple',
            len(docnames),
            self.config.verbosity,
        ):
            # remove all inventory entries for that file
            self.events.emit('env-purge-doc', self.env, docname)
            self.env.clear_doc(docname)
            self.read_doc(docname)

    def _read_parallel(self, docnames: list[str], nproc: int) -> None:
        chunks = make_chunks(docnames, nproc)

        # create a status_iterator to step progressbar after reading a document
        # (see: ``merge()`` function)
        progress = status_iterator(
            chunks,
            __('reading sources... '),
            'purple',
            len(chunks),
            self.config.verbosity,
        )

        # clear all outdated docs at once
        for docname in docnames:
            self.events.emit('env-purge-doc', self.env, docname)
            self.env.clear_doc(docname)

        def read_process(docs: list[str]) -> bytes:
            self.env._app = self._app
            for docname in docs:
                self.read_doc(docname, _cache=False)
            # allow pickling self to send it back
            return pickle.dumps(self.env, pickle.HIGHEST_PROTOCOL)

        def merge(docs: list[str], otherenv: bytes) -> None:
            env = pickle.loads(otherenv)
            self.env.merge_info_from(docs, env, self._app)

            next(progress)

        tasks = ParallelTasks(nproc)
        for chunk in chunks:
            tasks.add_task(read_process, chunk, merge)

        # make sure all threads have finished
        tasks.join()
        logger.info('')

    @final
    def read_doc(self, docname: str, *, _cache: bool = True) -> None:
        """Parse a file and add/update inventory entries for the doctree."""
        env = self.env
        env.prepare_settings(docname)

        # Add confdir/docutils.conf to dependencies list if exists
        docutils_conf = self.confdir / 'docutils.conf'
        if docutils_conf.is_file():
            env.note_dependency(docutils_conf)

        filename = env.doc2path(docname)

        # set up error_handler for the target document
        error_handler = _UnicodeDecodeErrorHandler(docname)
        codecs.register_error('sphinx', error_handler)  # type: ignore[arg-type]

        # read the source file
        content = filename.read_text(
            encoding=env.settings['input_encoding'], errors='sphinx'
        )

        # TODO: move the "source-read" event to here.

        filetype = get_filetype(self.config.source_suffix, filename)
        parser = self._registry.create_source_parser(
            filetype, config=self.config, env=env
        )
        doctree = _parse_str_to_doctree(
            content,
            filename=filename,
            default_role=self.config.default_role,
            default_settings=env.settings,
            env=env,
            events=self.events,
            parser=parser,
            transforms=self._registry.get_transforms(),
        )

        # store time of reading, for outdated files detection
        env.all_docs[docname] = time.time_ns() // 1_000

        # cleanup
        env.current_document = _CurrentDocument()
        env.ref_context.clear()

        self.write_doctree(docname, doctree, _cache=_cache)

    @final
    def write_doctree(
        self,
        docname: str,
        doctree: nodes.document,
        *,
        _cache: bool = True,
    ) -> None:
        """Write the doctree to a file, to be used as a cache by re-builds."""
        # make it pickleable
        doctree.reporter = None  # type: ignore[assignment]
        doctree.transformer = None  # type: ignore[assignment]

        # Create a copy of settings object before modification because it is
        # shared with other documents.
        doctree.settings = doctree.settings.copy()
        doctree.settings.warning_stream = None
        doctree.settings.env = None
        doctree.settings.record_dependencies = None

        doctree_filename = self.doctreedir / f'{docname}.doctree'
        doctree_filename.parent.mkdir(parents=True, exist_ok=True)
        with open(doctree_filename, 'wb') as f:
            pickle.dump(doctree, f, pickle.HIGHEST_PROTOCOL)

        # When Sphinx is running in parallel mode, ``write_doctree()`` is invoked
        # in the context of a process worker, and thus it does not make sense to
        # pickle the doctree and send it to the main process
        if _cache:
            self.env._write_doc_doctree_cache[docname] = doctree

    @final
    def write(
        self,
        build_docnames: Iterable[str] | None,
        updated_docnames: Iterable[str],
        method: Literal['all', 'specific', 'update'] = 'update',
    ) -> None:
        """Write builder specific output files."""
        env = self.env

        # Allow any extensions to perform setup for writing
        self.events.emit('write-started', self)

        if build_docnames is None or build_docnames == ['__all__']:
            # build_all
            build_docnames = env.found_docs
        if method == 'update':
            # build updated ones as well
            docnames = set(build_docnames) | set(updated_docnames)
        else:
            docnames = set(build_docnames)
        if docnames:
            logger.debug(__('docnames to write: %s'), ', '.join(sorted(docnames)))
        else:
            logger.debug(__('no docnames to write!'))

        # add all toctree-containing files that may have changed
        docnames |= {
            toc_docname
            for docname in docnames
            for toc_docname in env.files_to_rebuild.get(docname, ())
            if toc_docname in env.found_docs
        }

        # sort to ensure deterministic toctree generation
        env.toctree_includes = dict(sorted(env.toctree_includes.items()))

        with progress_message(__('preparing documents')):
            self.prepare_writing(docnames)

        with progress_message(__('copying assets'), nonl=False):
            self.copy_assets()

        if docnames:
            self.write_documents(docnames)

    def write_documents(self, docnames: Set[str]) -> None:
        """Write all documents in *docnames*.

        This method can be overridden if a builder does not create
        output files for each document.
        """
        sorted_docnames = sorted(docnames)
        if self.parallel_ok:
            # number of subprocesses is parallel-1 because the main process
            # is busy loading doctrees and doing write_doc_serialized()
            self._write_parallel(sorted_docnames, nproc=self._app.parallel - 1)
        else:
            self._write_serial(sorted_docnames)

    def _write_serial(self, docnames: Sequence[str]) -> None:
        with (
            nullcontext()
            if self._app._exception_on_warning
            else logging.pending_warnings()
        ):
            for docname in status_iterator(
                docnames,
                __('writing output... '),
                'darkgreen',
                len(docnames),
                self.config.verbosity,
            ):
                _write_docname(docname, env=self.env, builder=self, tags=self.tags)

    def _write_parallel(self, docnames: Sequence[str], nproc: int) -> None:
        def write_process(docs: list[tuple[str, nodes.document]]) -> None:
            self.phase = BuildPhase.WRITING
            for docname, doctree in docs:
                self.write_doc(docname, doctree)

        # warm up caches/compile templates using the first document
        firstname, docnames = docnames[0], docnames[1:]
        _write_docname(firstname, env=self.env, builder=self, tags=self.tags)

        tasks = ParallelTasks(nproc)
        chunks = make_chunks(docnames, nproc)

        # create a status_iterator to step progressbar after writing a document
        # (see: ``on_chunk_done()`` function)
        progress = status_iterator(
            chunks,
            __('writing output... '),
            'darkgreen',
            len(chunks),
            self.config.verbosity,
        )

        def on_chunk_done(args: list[tuple[str, nodes.document]], result: None) -> None:
            next(progress)

        self.phase = BuildPhase.RESOLVING
        for chunk in chunks:
            arg = []
            for docname in chunk:
                doctree = self.env.get_and_resolve_doctree(
                    docname, self, tags=self.tags
                )
                self.write_doc_serialized(docname, doctree)
                arg.append((docname, doctree))
            tasks.add_task(write_process, arg, on_chunk_done)

        # make sure all threads have finished
        tasks.join()
        logger.info('')

    def prepare_writing(self, docnames: Set[str]) -> None:
        """A place where you can add logic before :meth:`write_doc` is run"""
        pass

    def copy_assets(self) -> None:
        """Where assets (images, static files, etc) are copied before writing"""
        pass

    def write_doc(self, docname: str, doctree: nodes.document) -> None:
        """Write the output file for the document

        :param docname: the :term:`docname <document name>`.
        :param doctree: defines the content to be written.

        The output filename must be determined within this method,
        typically by calling :meth:`~.Builder.get_target_uri`
        or :meth:`~.Builder.get_relative_uri`.
        """
        raise NotImplementedError

    def write_doc_serialized(self, docname: str, doctree: nodes.document) -> None:
        """Handle parts of write_doc that must be called in the main process
        if parallel build is active.
        """
        pass

    def finish(self) -> None:
        """Finish the building process.

        The default implementation does nothing.
        """
        pass

    def cleanup(self) -> None:
        """Cleanup any resources.

        The default implementation does nothing.
        """
        pass

    def get_builder_config(self, option: str, default: str) -> Any:
        """Return a builder specific option.

        This method allows customization of common builder settings by
        inserting the name of the current builder in the option key.
        If the key does not exist, use default as builder name.
        """
        # At the moment, only XXX_use_index is looked up this way.
        # Every new builder variant must be registered in Config.config_values.
        try:
            optname = f'{self.name}_{option}'
            return getattr(self.config, optname)
        except AttributeError:
            optname = f'{default}_{option}'
            return getattr(self.config, optname)


def _write_docname(
    docname: str,
    /,
    *,
    env: BuildEnvironment,
    builder: Builder,
    tags: Tags,
) -> None:
    """Write a single document."""
    builder.phase = BuildPhase.RESOLVING
    doctree = env.get_and_resolve_doctree(docname, builder=builder, tags=tags)
    builder.phase = BuildPhase.WRITING
    builder.write_doc_serialized(docname, doctree)
    builder.write_doc(docname, doctree)


class _UnicodeDecodeErrorHandler:
    """Custom error handler for open() that warns and replaces."""

    def __init__(self, docname: str, /) -> None:
        self.docname = docname

    def __call__(self, error: UnicodeDecodeError) -> tuple[str, int]:
        line_start = error.object.rfind(b'\n', 0, error.start)
        line_end = error.object.find(b'\n', error.start)
        if line_end == -1:
            line_end = len(error.object)
        line_num = error.object.count(b'\n', 0, error.start) + 1
        logger.warning(
            __('undecodable source characters, replacing with "?": %r'),
            (
                error.object[line_start + 1 : error.start]
                + b'>>>'
                + error.object[error.start : error.end]
                + b'<<<'
                + error.object[error.end : line_end]
            ),
            location=(self.docname, line_num),
        )
        return '?', error.end
