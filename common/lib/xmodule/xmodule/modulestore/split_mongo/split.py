"""
Provides full versioning CRUD and representation for collections of xblocks (e.g., courses, modules, etc).

Representation:
* course_index: a dictionary:
    ** '_id': package_id (e.g., myu.mydept.mycourse.myrun),
    ** 'org': the org's id. Only used for searching not identity,
    ** 'prettyid': a vague to-be-determined field probably more useful to storing searchable tags,
    ** 'edited_by': user_id of user who created the original entry,
    ** 'edited_on': the datetime of the original creation,
    ** 'versions': versions_dict: {branch_id: structure_id, ...}
* structure:
    ** '_id': an ObjectId (guid),
    ** 'root': root_block_id (string of key in 'blocks' for the root of this structure,
    ** 'previous_version': the structure from which this one was derived. For published courses, this
    points to the previously published version of the structure not the draft published to this.
    ** 'original_version': the original structure id in the previous_version relation. Is a pseudo object
    identifier enabling quick determination if 2 structures have any shared history,
    ** 'edited_by': user_id of the user whose change caused the creation of this structure version,
    ** 'edited_on': the datetime for the change causing this creation of this structure version,
    ** 'blocks': dictionary of xblocks in this structure:
        *** block_id: dictionary of block settings and children:
            **** 'category': the xblock type id
            **** 'definition': the db id of the record containing the content payload for this xblock
            **** 'fields': the Scope.settings and children field values
            **** 'edit_info': dictionary:
                ***** 'edited_on': when was this xblock's fields last changed (will be edited_on value of
                update_version structure)
                ***** 'edited_by': user_id for who changed this xblock last (will be edited_by value of
                update_version structure)
                ***** 'update_version': the guid for the structure where this xblock got its current field
                values. This may point to a structure not in this structure's history (e.g., to a draft
                branch from which this version was published.)
                ***** 'previous_version': the guid for the structure which previously changed this xblock
                (will be the previous value of update_version; so, may point to a structure not in this
                structure's history.)
* definition: shared content with revision history for xblock content fields
    ** '_id': definition_id (guid),
    ** 'category': xblock type id
    ** 'fields': scope.content (and possibly other) field values.
    ** 'edit_info': dictionary:
        *** 'edited_by': user_id whose edit caused this version of the definition,
        *** 'edited_on': datetime of the change causing this version
        *** 'previous_version': the definition_id of the previous version of this definition
        *** 'original_version': definition_id of the root of the previous version relation on this
        definition. Acts as a pseudo-object identifier.
"""
import threading
import datetime
import logging
import re
from importlib import import_module
from path import path
import collections
import copy
from pytz import UTC

from xmodule.errortracker import null_error_tracker
from xmodule.x_module import prefer_xmodules
from xmodule.modulestore.locator import (
    BlockUsageLocator, DefinitionLocator, CourseLocator, VersionTree, LocalId, Locator
)
from xmodule.modulestore.exceptions import InsufficientSpecificationError, VersionConflictError, DuplicateItemError
from xmodule.modulestore import inheritance, ModuleStoreWriteBase, Location, SPLIT_MONGO_MODULESTORE_TYPE

from ..exceptions import ItemNotFoundError
from .definition_lazy_loader import DefinitionLazyLoader
from .caching_descriptor_system import CachingDescriptorSystem
from xblock.fields import Scope
from xblock.runtime import Mixologist
from bson.objectid import ObjectId
from xmodule.modulestore.split_mongo.mongo_connection import MongoConnection
from xblock.core import XBlock
from xmodule.modulestore.loc_mapper_store import LocMapperStore

log = logging.getLogger(__name__)
#==============================================================================
# Documentation is at
# https://edx-wiki.atlassian.net/wiki/display/ENG/Mongostore+Data+Structure
#
# Known issue:
#    Inheritance for cached kvs doesn't work on edits. Use case.
#     1) attribute foo is inheritable
#     2) g.children = [p], p.children = [a]
#     3) g.foo = 1 on load
#     4) if g.foo > 0, if p.foo > 0, if a.foo > 0 all eval True
#     5) p.foo = -1
#     6) g.foo > 0, p.foo <= 0 all eval True BUT
#     7) BUG: a.foo > 0 still evals True but should be False
#     8) reread and everything works right
#     9) p.del(foo), p.foo > 0 is True! works
#    10) BUG: a.foo < 0!
#   Local fix wont' permanently work b/c xblock may cache a.foo...
#
#==============================================================================


class SplitMongoModuleStore(ModuleStoreWriteBase):
    """
    A Mongodb backed ModuleStore supporting versions, inheritance,
    and sharing.
    """
    reference_type = Locator
    def __init__(self, doc_store_config, fs_root, render_template,
                 default_class=None,
                 error_tracker=null_error_tracker,
                 loc_mapper=None,
                 **kwargs):
        """
        :param doc_store_config: must have a host, db, and collection entries. Other common entries: port, tz_aware.
        """

        super(SplitMongoModuleStore, self).__init__(**kwargs)
        self.loc_mapper = loc_mapper

        self.db_connection = MongoConnection(**doc_store_config)
        self.db = self.db_connection.database

        # Code review question: How should I expire entries?
        # _add_cache could use a lru mechanism to control the cache size?
        self.thread_cache = threading.local()

        if default_class is not None:
            module_path, _, class_name = default_class.rpartition('.')
            class_ = getattr(import_module(module_path), class_name)
            self.default_class = class_
        else:
            self.default_class = None
        self.fs_root = path(fs_root)
        self.error_tracker = error_tracker
        self.render_template = render_template

        # TODO: Don't have a runtime just to generate the appropriate mixin classes (cpennington)
        # This is only used by _partition_fields_by_scope, which is only needed because
        # the split mongo store is used for item creation as well as item persistence
        self.mixologist = Mixologist(self.xblock_mixins)

    def cache_items(self, system, base_block_ids, depth=0, lazy=True):
        '''
        Handles caching of items once inheritance and any other one time
        per course per fetch operations are done.
        :param system: a CachingDescriptorSystem
        :param base_block_ids: list of block_ids to fetch
        :param depth: how deep below these to prefetch
        :param lazy: whether to fetch definitions or use placeholders
        '''
        new_module_data = {}
        for block_id in base_block_ids:
            new_module_data = self.descendants(
                system.course_entry['structure']['blocks'],
                block_id,
                depth,
                new_module_data
            )

        if lazy:
            for block in new_module_data.itervalues():
                block['definition'] = DefinitionLazyLoader(self, block['definition'])
        else:
            # Load all descendants by id
            descendent_definitions = self.db_connection.find_matching_definitions({
                '_id': {'$in': [block['definition']
                                for block in new_module_data.itervalues()]}})
            # turn into a map
            definitions = {definition['_id']: definition
                           for definition in descendent_definitions}

            for block in new_module_data.itervalues():
                if block['definition'] in definitions:
                    block['fields'].update(definitions[block['definition']].get('fields'))

        system.module_data.update(new_module_data)
        return system.module_data

    def _load_items(self, course_entry, block_ids, depth=0, lazy=True):
        '''
        Load & cache the given blocks from the course. Prefetch down to the
        given depth. Load the definitions into each block if lazy is False;
        otherwise, use the lazy definition placeholder.
        '''
        system = self._get_cache(course_entry['structure']['_id'])
        if system is None:
            system = CachingDescriptorSystem(
                modulestore=self,
                course_entry=course_entry,
                module_data={},
                lazy=lazy,
                default_class=self.default_class,
                error_tracker=self.error_tracker,
                render_template=self.render_template,
                resources_fs=None,
                mixins=self.xblock_mixins,
                select=self.xblock_select,
            )
            self._add_cache(course_entry['structure']['_id'], system)
            self.cache_items(system, block_ids, depth, lazy)
        return [system.load_item(block_id, course_entry) for block_id in block_ids]

    def _get_cache(self, course_version_guid):
        """
        Find the descriptor cache for this course if it exists
        :param course_version_guid:
        """
        if not hasattr(self.thread_cache, 'course_cache'):
            self.thread_cache.course_cache = {}
        system = self.thread_cache.course_cache
        return system.get(course_version_guid)

    def _add_cache(self, course_version_guid, system):
        """
        Save this cache for subsequent access
        :param course_version_guid:
        :param system:
        """
        if not hasattr(self.thread_cache, 'course_cache'):
            self.thread_cache.course_cache = {}
        self.thread_cache.course_cache[course_version_guid] = system
        return system

    def _clear_cache(self, course_version_guid=None):
        """
        Should only be used by testing or something which implements transactional boundary semantics.
        :param course_version_guid: if provided, clear only this entry
        """
        if course_version_guid:
            del self.thread_cache.course_cache[course_version_guid]
        else:
            self.thread_cache.course_cache = {}

    def _lookup_course(self, course_locator):
        '''
        Decode the locator into the right series of db access. Does not
        return the CourseDescriptor! It returns the actual db json from
        structures.

        Semantics: if package_id and branch given, then it will get that branch. If
        also give a version_guid, it will see if the current head of that branch == that guid. If not
        it raises VersionConflictError (the version now differs from what it was when you got your
        reference)

        :param course_locator: any subclass of CourseLocator
        '''
        # NOTE: if and when this uses cache, the update if changed logic will break if the cache
        # holds the same objects as the descriptors!
        if not course_locator.is_fully_specified():
            raise InsufficientSpecificationError('Not fully specified: %s' % course_locator)

        if course_locator.package_id is not None and course_locator.branch is not None:
            # use the package_id
            index = self.db_connection.get_course_index(course_locator.package_id)
            if index is None:
                raise ItemNotFoundError(course_locator)
            if course_locator.branch not in index['versions']:
                raise ItemNotFoundError(course_locator)
            version_guid = index['versions'][course_locator.branch]
            if course_locator.version_guid is not None and version_guid != course_locator.version_guid:
                # This may be a bit too touchy but it's hard to infer intent
                raise VersionConflictError(course_locator, version_guid)
        else:
            # TODO should this raise an exception if branch was provided?
            version_guid = course_locator.version_guid

        # cast string to ObjectId if necessary
        version_guid = course_locator.as_object_id(version_guid)
        entry = self.db_connection.get_structure(version_guid)

        # b/c more than one course can use same structure, the 'package_id' and 'branch' are not intrinsic to structure
        # and the one assoc'd w/ it by another fetch may not be the one relevant to this fetch; so,
        # add it in the envelope for the structure.
        envelope = {
            'package_id': course_locator.package_id,
            'branch': course_locator.branch,
            'structure': entry,
        }
        return envelope

    def get_courses(self, branch='published', qualifiers=None):
        '''
        Returns a list of course descriptors matching any given qualifiers.

        qualifiers should be a dict of keywords matching the db fields or any
        legal query for mongo to use against the active_versions collection.

        Note, this is to find the current head of the named branch type
        (e.g., 'draft'). To get specific versions via guid use get_course.

        :param branch: the branch for which to return courses. Default value is 'published'.
        :param qualifiers: a optional dict restricting which elements should match
        '''
        if qualifiers is None:
            qualifiers = {}
        qualifiers.update({"versions.{}".format(branch): {"$exists": True}})
        matching = self.db_connection.find_matching_course_indexes(qualifiers)

        # collect ids and then query for those
        version_guids = []
        id_version_map = {}
        for structure in matching:
            version_guid = structure['versions'][branch]
            version_guids.append(version_guid)
            id_version_map[version_guid] = structure['_id']

        course_entries = self.db_connection.find_matching_structures({'_id': {'$in': version_guids}})

        # get the block for the course element (s/b the root)
        result = []
        for entry in course_entries:
            envelope = {
                'package_id': id_version_map[entry['_id']],
                'branch': branch,
                'structure': entry,
            }
            root = entry['root']
            result.extend(self._load_items(envelope, [root], 0, lazy=True))
        return result

    def get_course(self, course_locator):
        '''
        Gets the course descriptor for the course identified by the locator
        which may or may not be a blockLocator.

        raises InsufficientSpecificationError
        '''
        course_entry = self._lookup_course(course_locator)
        root = course_entry['structure']['root']
        result = self._load_items(course_entry, [root], 0, lazy=True)
        return result[0]

    def get_course_for_item(self, location):
        '''
        Provided for backward compatibility. Is equivalent to calling get_course
        :param location:
        '''
        return self.get_course(location)

    def has_item(self, package_id, block_location):
        """
        Returns True if location exists in its course. Returns false if
        the course or the block w/in the course do not exist for the given version.
        raises InsufficientSpecificationError if the locator does not id a block
        """
        if block_location.block_id is None:
            raise InsufficientSpecificationError(block_location)
        try:
            course_structure = self._lookup_course(block_location)['structure']
        except ItemNotFoundError:
            # this error only occurs if the course does not exist
            return False

        return self._get_block_from_structure(course_structure, block_location.block_id) is not None

    def get_item(self, location, depth=0):
        """
        depth (int): An argument that some module stores may use to prefetch
            descendants of the queried modules for more efficient results later
            in the request. The depth is counted in the number of
            calls to get_children() to cache. None indicates to cache all
            descendants.
        raises InsufficientSpecificationError or ItemNotFoundError
        """
        # intended for temporary support of some pointers being old-style
        if isinstance(location, Location):
            if self.loc_mapper is None:
                raise InsufficientSpecificationError('No location mapper configured')
            else:
                location = self.loc_mapper.translate_location(
                    None, location, location.revision is None,
                    add_entry_if_missing=False
                )
        assert isinstance(location, BlockUsageLocator)
        if not location.is_initialized():
            raise InsufficientSpecificationError("Not yet initialized: %s" % location)
        course = self._lookup_course(location)
        items = self._load_items(course, [location.block_id], depth, lazy=True)
        if len(items) == 0:
            raise ItemNotFoundError(location)
        return items[0]

    def get_items(self, locator, course_id=None, depth=0, qualifiers=None):
        """
        Get all of the modules in the given course matching the qualifiers. The
        qualifiers should only be fields in the structures collection (sorry).
        There will be a separate search method for searching through
        definitions.

        Common qualifiers are category, definition (provide definition id),
        display_name, anyfieldname, children (return
        block if its children includes the one given value). If you want
        substring matching use {$regex: /acme.*corp/i} type syntax.

        Although these
        look like mongo queries, it is all done in memory; so, you cannot
        try arbitrary queries.

        :param locator: CourseLocator or BlockUsageLocator restricting search scope
        :param course_id: ignored. Only included for API compatibility.
        :param depth: ignored. Only included for API compatibility.
        :param qualifiers: a dict restricting which elements should match

        """
        # TODO extend to only search a subdag of the course?
        if qualifiers is None:
            qualifiers = {}
        course = self._lookup_course(locator)
        items = []
        for block_id, value in course['structure']['blocks'].iteritems():
            if self._block_matches(value, qualifiers):
                items.append(block_id)

        if len(items) > 0:
            return self._load_items(course, items, 0, lazy=True)
        else:
            return []

    def get_instance(self, course_id, location, depth=0):
        """
        Get an instance of this location.

        For now, just delegate to get_item and ignore course policy.

        depth (int): An argument that some module stores may use to prefetch
            descendants of the queried modules for more efficient results later
            in the request. The depth is counted in the number of
            calls to get_children() to cache. None indicates to cache all descendants.
        """
        return self.get_item(location, depth=depth)

    def get_parent_locations(self, locator, course_id=None):
        '''
        Return the locations (Locators w/ block_ids) for the parents of this location in this
        course. Could use get_items(location, {'children': block_id}) but this is slightly faster.
        NOTE: the locator must contain the block_id, and this code does not actually ensure block_id exists

        :param locator: BlockUsageLocator restricting search scope
        :param course_id: ignored. Only included for API compatibility. Specify the course_id within the locator.
        '''
        course = self._lookup_course(locator)
        items = self._get_parents_from_structure(locator.block_id, course['structure'])
        return [BlockUsageLocator(
                    url=locator.as_course_locator(),
                    block_id=LocMapperStore.decode_key_from_mongo(parent_id),
                )
                for parent_id in items]

    def get_orphans(self, package_id, branch):
        """
        Return a dict of all of the orphans in the course.

        :param package_id:
        """
        detached_categories = [name for name, __ in XBlock.load_tagged_classes("detached")]
        course = self._lookup_course(CourseLocator(package_id=package_id, branch=branch))
        items = {LocMapperStore.decode_key_from_mongo(block_id) for block_id in course['structure']['blocks'].keys()}
        items.remove(course['structure']['root'])
        for block_id, block_data in course['structure']['blocks'].iteritems():
            items.difference_update(block_data.get('fields', {}).get('children', []))
            if block_data['category'] in detached_categories:
                items.discard(LocMapperStore.decode_key_from_mongo(block_id))
        return [
            BlockUsageLocator(package_id=package_id, branch=branch, block_id=block_id)
            for block_id in items
        ]

    def get_course_index_info(self, course_locator):
        """
        The index records the initial creation of the indexed course and tracks the current version
        heads. This function is primarily for test verification but may serve some
        more general purpose.
        :param course_locator: must have a package_id set
        :return {'org': , 'prettyid': ,
            versions: {'draft': the head draft version id,
                'published': the head published version id if any,
            },
            'edited_by': who created the course originally (named edited for consistency),
            'edited_on': when the course was originally created
        }
        """
        if course_locator.package_id is None:
            return None
        index = self.db_connection.get_course_index(course_locator.package_id)
        return index

    # TODO figure out a way to make this info accessible from the course descriptor
    def get_course_history_info(self, course_locator):
        """
        Because xblocks doesn't give a means to separate the course structure's meta information from
        the course xblock's, this method will get that info for the structure as a whole.
        :param course_locator:
        :return {'original_version': the version guid of the original version of this course,
            'previous_version': the version guid of the previous version,
            'edited_by': who made the last change,
            'edited_on': when the change was made
        }
        """
        course = self._lookup_course(course_locator)['structure']
        return {
            'original_version': course['original_version'],
            'previous_version': course['previous_version'],
            'edited_by': course['edited_by'],
            'edited_on': course['edited_on']
        }

    def get_definition_history_info(self, definition_locator):
        """
        Because xblocks doesn't give a means to separate the definition's meta information from
        the usage xblock's, this method will get that info for the definition
        :return {'original_version': the version guid of the original version of this course,
            'previous_version': the version guid of the previous version,
            'edited_by': who made the last change,
            'edited_on': when the change was made
        }
        """
        definition = self.db_connection.get_definition(definition_locator.definition_id)
        if definition is None:
            return None
        return definition['edit_info']

    def get_course_successors(self, course_locator, version_history_depth=1):
        '''
        Find the version_history_depth next versions of this course. Return as a VersionTree
        Mostly makes sense when course_locator uses a version_guid, but because it finds all relevant
        next versions, these do include those created for other courses.
        :param course_locator:
        '''
        if version_history_depth < 1:
            return None
        if course_locator.version_guid is None:
            course = self._lookup_course(course_locator)
            version_guid = course['structure']['_id']
        else:
            version_guid = course_locator.version_guid

        # TODO if depth is significant, it may make sense to get all that have the same original_version
        # and reconstruct the subtree from version_guid
        next_entries = self.db_connection.find_matching_structures({'previous_version' : version_guid})
        # must only scan cursor's once
        next_versions = [struct for struct in next_entries]
        result = {version_guid: [CourseLocator(version_guid=struct['_id']) for struct in next_versions]}
        depth = 1
        while depth < version_history_depth and len(next_versions) > 0:
            depth += 1
            next_entries = self.db_connection.find_matching_structures({'previous_version':
                {'$in': [struct['_id'] for struct in next_versions]}})
            next_versions = [struct for struct in next_entries]
            for course_structure in next_versions:
                result.setdefault(course_structure['previous_version'], []).append(
                    CourseLocator(version_guid=struct['_id']))
        return VersionTree(CourseLocator(course_locator, version_guid=version_guid), result)


    def get_block_generations(self, block_locator):
        '''
        Find the history of this block. Return as a VersionTree of each place the block changed (except
        deletion).

        The block's history tracks its explicit changes but not the changes in its children.

        '''
        # version_agnostic means we don't care if the head and version don't align, trust the version
        course_struct = self._lookup_course(block_locator.version_agnostic())['structure']
        block_id = block_locator.block_id
        update_version_field = 'blocks.{}.edit_info.update_version'.format(block_id)
        all_versions_with_block = self.db_connection.find_matching_structures({'original_version': course_struct['original_version'],
            update_version_field: {'$exists': True}})
        # find (all) root versions and build map {previous: {successors}..}
        possible_roots = []
        result = {}
        for version in all_versions_with_block:
            block_payload = self._get_block_from_structure(version, block_id)
            if version['_id'] == block_payload['edit_info']['update_version']:
                if block_payload['edit_info'].get('previous_version') is None:
                    possible_roots.append(block_payload['edit_info']['update_version'])
                else:  # map previous to {update..}
                    result.setdefault(block_payload['edit_info']['previous_version'], set()).add(
                        block_payload['edit_info']['update_version'])

        # more than one possible_root means usage was added and deleted > 1x.
        if len(possible_roots) > 1:
            # find the history segment including block_locator's version
            element_to_find = self._get_block_from_structure(course_struct, block_id)['edit_info']['update_version']
            if element_to_find in possible_roots:
                possible_roots = [element_to_find]
            for possibility in possible_roots:
                if self._find_local_root(element_to_find, possibility, result):
                    possible_roots = [possibility]
                    break
        elif len(possible_roots) == 0:
            return None
        # convert the results value sets to locators
        for k, versions in result.iteritems():
            result[k] = [BlockUsageLocator(version_guid=version, block_id=block_id)
                for version in versions]
        return VersionTree(BlockUsageLocator(version_guid=possible_roots[0], block_id=block_id), result)

    def get_definition_successors(self, definition_locator, version_history_depth=1):
        '''
        Find the version_history_depth next versions of this definition. Return as a VersionTree
        '''
        # TODO implement
        raise NotImplementedError()

    def create_definition_from_data(self, new_def_data, category, user_id):
        """
        Pull the definition fields out of descriptor and save to the db as a new definition
        w/o a predecessor and return the new id.

        :param user_id: request.user object
        """
        new_def_data = self._filter_special_fields(new_def_data)
        new_id = ObjectId()
        document = {
            '_id': new_id,
            "category" : category,
            "fields": new_def_data,
            "edit_info": {
                "edited_by": user_id,
                "edited_on": datetime.datetime.now(UTC),
                "previous_version": None,
                "original_version": new_id,
            }
        }
        self.db_connection.insert_definition(document)
        definition_locator = DefinitionLocator(new_id)
        return definition_locator

    def update_definition_from_data(self, definition_locator, new_def_data, user_id):
        """
        See if new_def_data differs from the persisted version. If so, update
        the persisted version and return the new id.

        :param user_id: request.user
        """
        new_def_data = self._filter_special_fields(new_def_data)
        def needs_saved():
            for key, value in new_def_data.iteritems():
                if key not in old_definition['fields'] or value != old_definition['fields'][key]:
                    return True
            for key, value in old_definition.get('fields', {}).iteritems():
                if key not in new_def_data:
                    return True

        # if this looks in cache rather than fresh fetches, then it will probably not detect
        # actual change b/c the descriptor and cache probably point to the same objects
        old_definition = self.db_connection.get_definition(definition_locator.definition_id)
        if old_definition is None:
            raise ItemNotFoundError(definition_locator.url())

        if needs_saved():
            # new id to create new version
            old_definition['_id'] = ObjectId()
            old_definition['fields'] = new_def_data
            old_definition['edit_info']['edited_by'] = user_id
            old_definition['edit_info']['edited_on'] = datetime.datetime.now(UTC)
            # previous version id
            old_definition['edit_info']['previous_version'] = definition_locator.definition_id
            self.db_connection.insert_definition(old_definition)
            return DefinitionLocator(old_definition['_id']), True
        else:
            return definition_locator, False

    def _generate_block_id(self, course_blocks, category):
        """
        Generate a somewhat readable block id unique w/in this course using the category
        :param course_blocks: the current list of blocks.
        :param category:
        """
        # NOTE: a potential bug is that a block is deleted and another created which gets the old
        # block's id. a possible fix is to cache the last serial in a dict in the structure
        # {category: last_serial...}
        # A potential confusion is if the name incorporates the parent's name, then if the child
        # moves, its id won't change and will be confusing
        # NOTE2: this assumes category will never contain a $ nor a period.
        serial = 1
        while category + str(serial) in course_blocks:
            serial += 1
        return category + str(serial)

    def _generate_package_id(self, id_root):
        """
        Generate a somewhat readable course id unique w/in this db using the id_root
        :param course_blocks: the current list of blocks.
        :param category:
        """
        existing_uses = self.db_connection.find_matching_course_indexes({"_id": {"$regex": id_root}})
        if existing_uses.count() > 0:
            max_found = 0
            matcher = re.compile(id_root + r'(\d+)')
            for entry in existing_uses:
                serial = re.search(matcher, entry['_id'])
                if serial is not None and serial.groups > 0:
                    value = int(serial.group(1))
                    if value > max_found:
                        max_found = value
            return id_root + str(max_found + 1)
        else:
            return id_root

    # DHM: Should I rewrite this to take a new xblock instance rather than to construct it? That is, require the
    # caller to use XModuleDescriptor.load_from_json thus reducing similar code and making the object creation and
    # validation behavior a responsibility of the model layer rather than the persistence layer.
    def create_item(
        self, course_or_parent_locator, category, user_id,
        block_id=None, definition_locator=None, fields=None,
        force=False, continue_version=False
    ):
        """
        Add a descriptor to persistence as the last child of the optional parent_location or just as an element
        of the course (if no parent provided). Return the resulting post saved version with populated locators.

        :param course_or_parent_locator: If BlockUsageLocator, then it's assumed to be the parent.
        If it's a CourseLocator, then it's
        merely the containing course.

        raises InsufficientSpecificationError if there is no course locator.
        raises VersionConflictError if package_id and version_guid given and the current version head != version_guid
            and force is not True.
        :param force: fork the structure and don't update the course draftVersion if the above
        :param continue_revision: for multistep transactions, continue revising the given version rather than creating
        a new version. Setting force to True conflicts with setting this to True and will cause a VersionConflictError

        :param definition_locator: should either be None to indicate this is a brand new definition or
        a pointer to the existing definition to which this block should point or from which this was derived
        or a LocalId to indicate that it's new.
        If fields does not contain any Scope.content, then definition_locator must have a value meaning that this
        block points
        to the existing definition. If fields contains Scope.content and definition_locator is not None, then
        the Scope.content fields are assumed to be a new payload for definition_locator.

        :param block_id: if provided, must not already exist in the structure. Provides the block id for the
        new item in this structure. Otherwise, one is computed using the category appended w/ a few digits.

        :param continue_version: continue changing the current structure at the head of the course. Very dangerous
        unless used in the same request as started the change! See below about version conflicts.

        This method creates a new version of the course structure unless continue_version is True.
        It creates and inserts the new block, makes the block point
        to the definition which may be new or a new version of an existing or an existing.

        Rules for course locator:

        * If the course locator specifies a package_id and either it doesn't
          specify version_guid or the one it specifies == the current head of the branch,
          it progresses the course to point
          to the new head and sets the active version to point to the new head
        * If the locator has a package_id but its version_guid != current head, it raises VersionConflictError.

        NOTE: using a version_guid will end up creating a new version of the course. Your new item won't be in
        the course id'd by version_guid but instead in one w/ a new version_guid. Ensure in this case that you get
        the new version_guid from the locator in the returned object!
        """
        # find course_index entry if applicable and structures entry
        index_entry = self._get_index_if_valid(course_or_parent_locator, force, continue_version)
        structure = self._lookup_course(course_or_parent_locator)['structure']

        partitioned_fields = self._partition_fields_by_scope(category, fields)
        new_def_data = partitioned_fields.get(Scope.content, {})
        # persist the definition if persisted != passed
        if (definition_locator is None or isinstance(definition_locator.definition_id, LocalId)):
            definition_locator = self.create_definition_from_data(new_def_data, category, user_id)
        elif new_def_data is not None:
            definition_locator, _ = self.update_definition_from_data(definition_locator, new_def_data, user_id)

        # copy the structure and modify the new one
        if continue_version:
            new_structure = structure
        else:
            new_structure = self._version_structure(structure, user_id)

        new_id = new_structure['_id']

        # generate usage id
        if block_id is not None:
            if LocMapperStore.encode_key_for_mongo(block_id) in new_structure['blocks']:
                raise DuplicateItemError(block_id, self, 'structures')
            else:
                new_block_id = block_id
        else:
            new_block_id = self._generate_block_id(new_structure['blocks'], category)

        block_fields = partitioned_fields.get(Scope.settings, {})
        if Scope.children in partitioned_fields:
            block_fields.update(partitioned_fields[Scope.children])
        self._update_block_in_structure(new_structure, new_block_id, {
            "category": category,
            "definition": definition_locator.definition_id,
            "fields": block_fields,
            'edit_info': {
                'edited_on': datetime.datetime.now(UTC),
                'edited_by': user_id,
                'previous_version': None,
                'update_version': new_id,
            }
        })

        # if given parent, add new block as child and update parent's version
        parent = None
        if isinstance(course_or_parent_locator, BlockUsageLocator) and course_or_parent_locator.block_id is not None:
            encoded_block_id = LocMapperStore.encode_key_for_mongo(course_or_parent_locator.block_id)
            parent = new_structure['blocks'][encoded_block_id]
            parent['fields'].setdefault('children', []).append(new_block_id)
            if not continue_version or parent['edit_info']['update_version'] != structure['_id']:
                parent['edit_info']['edited_on'] = datetime.datetime.now(UTC)
                parent['edit_info']['edited_by'] = user_id
                parent['edit_info']['previous_version'] = parent['edit_info']['update_version']
                parent['edit_info']['update_version'] = new_id
        if continue_version:
            # db update
            self.db_connection.update_structure(new_structure)
            # clear cache so things get refetched and inheritance recomputed
            self._clear_cache(new_id)
        else:
            self.db_connection.insert_structure(new_structure)

        # update the index entry if appropriate
        if index_entry is not None:
            if not continue_version:
                self._update_head(index_entry, course_or_parent_locator.branch, new_id)
            course_parent = course_or_parent_locator.as_course_locator()
        else:
            course_parent = None

        # reconstruct the new_item from the cache
        return self.get_item(BlockUsageLocator(package_id=course_parent,
                                               block_id=new_block_id,
                                               version_guid=new_id))

    def create_course(
        self, org, prettyid, user_id, id_root=None, fields=None,
        master_branch='draft', versions_dict=None, root_category='course',
        root_block_id='course'
    ):
        """
        Create a new entry in the active courses index which points to an existing or new structure. Returns
        the course root of the resulting entry (the location has the course id)

        id_root: allows the caller to specify the package_id. It's a root in that, if it's already taken,
        this method will append things to the root to make it unique. (defaults to org)

        fields: if scope.settings fields provided, will set the fields of the root course object in the
        new course. If both
        settings fields and a starting version are provided (via versions_dict), it will generate a successor version
        to the given version,
        and update the settings fields with any provided values (via update not setting).

        fields (content): if scope.content fields provided, will update the fields of the new course
        xblock definition to this. Like settings fields,
        if provided, this will cause a new version of any given version as well as a new version of the
        definition (which will point to the existing one if given a version). If not provided and given
        a version_dict, it will reuse the same definition as that version's course
        (obvious since it's reusing the
        course). If not provided and no version_dict is given, it will be empty and get the field defaults
        when
        loaded.

        master_branch: the tag (key) for the version name in the dict which is the 'draft' version. Not the actual
        version guid, but what to call it.

        versions_dict: the starting version ids where the keys are the tags such as 'draft' and 'published'
        and the values are structure guids. If provided, the new course will reuse this version (unless you also
        provide any fields overrides, see above). if not provided, will create a mostly empty course
        structure with just a category course root xblock.
        """
        partitioned_fields = self._partition_fields_by_scope(root_category, fields)
        block_fields = partitioned_fields.setdefault(Scope.settings, {})
        if Scope.children in partitioned_fields:
            block_fields.update(partitioned_fields[Scope.children])
        definition_fields = self._filter_special_fields(partitioned_fields.get(Scope.content, {}))

        # build from inside out: definition, structure, index entry
        # if building a wholly new structure
        if versions_dict is None or master_branch not in versions_dict:
            # create new definition and structure
            definition_id = ObjectId()
            definition_entry = {
                '_id': definition_id,
                'category': root_category,
                'fields': definition_fields,
                'edit_info': {
                    'edited_by': user_id,
                    'edited_on': datetime.datetime.now(UTC),
                    'previous_version': None,
                    'original_version': definition_id,
                }
            }
            self.db_connection.insert_definition(definition_entry)

            draft_structure = self._new_structure(
                user_id, root_block_id, root_category, block_fields, definition_id
            )
            new_id = draft_structure['_id']

            self.db_connection.insert_structure(draft_structure)

            if versions_dict is None:
                versions_dict = {master_branch: new_id}
            else:
                versions_dict[master_branch] = new_id

        else:
            # just get the draft_version structure
            draft_version = CourseLocator(version_guid=versions_dict[master_branch])
            draft_structure = self._lookup_course(draft_version)['structure']
            if definition_fields or block_fields:
                draft_structure = self._version_structure(draft_structure, user_id)
                new_id = draft_structure['_id']
                encoded_block_id = LocMapperStore.encode_key_for_mongo(draft_structure['root'])
                root_block = draft_structure['blocks'][encoded_block_id]
                if block_fields is not None:
                    root_block['fields'].update(block_fields)
                if definition_fields is not None:
                    definition = self.db_connection.get_definition(root_block['definition'])
                    definition['fields'].update(definition_fields)
                    definition['edit_info']['previous_version'] = definition['_id']
                    definition['edit_info']['edited_by'] = user_id
                    definition['edit_info']['edited_on'] = datetime.datetime.now(UTC)
                    definition['_id'] = ObjectId()
                    self.db_connection.insert_definition(definition)
                    root_block['definition'] = definition['_id']
                    root_block['edit_info']['edited_on'] = datetime.datetime.now(UTC)
                    root_block['edit_info']['edited_by'] = user_id
                    root_block['edit_info']['previous_version'] = root_block['edit_info'].get('update_version')
                    root_block['edit_info']['update_version'] = new_id

                self.db_connection.insert_structure(draft_structure)
                versions_dict[master_branch] = new_id

        # create the index entry
        if id_root is None:
            id_root = org
        new_id = self._generate_package_id(id_root)

        index_entry = {
            '_id': new_id,
            'org': org,
            'prettyid': prettyid,
            'edited_by': user_id,
            'edited_on': datetime.datetime.now(UTC),
            'versions': versions_dict}
        self.db_connection.insert_course_index(index_entry)
        return self.get_course(CourseLocator(package_id=new_id, branch=master_branch))

    def update_item(self, descriptor, user_id, allow_not_found=False, force=False):
        """
        Save the descriptor's fields. it doesn't descend the course dag to save the children.
        Return the new descriptor (updated location).

        raises ItemNotFoundError if the location does not exist.

        Creates a new course version. If the descriptor's location has a package_id, it moves the course head
        pointer. If the version_guid of the descriptor points to a non-head version and there's been an intervening
        change to this item, it raises a VersionConflictError unless force is True. In the force case, it forks
        the course but leaves the head pointer where it is (this change will not be in the course head).

        The implementation tries to detect which, if any changes, actually need to be saved and thus won't version
        the definition, structure, nor course if they didn't change.
        """
        original_structure = self._lookup_course(descriptor.location)['structure']
        index_entry = self._get_index_if_valid(descriptor.location, force)

        descriptor.definition_locator, is_updated = self.update_definition_from_data(
            descriptor.definition_locator, descriptor.get_explicitly_set_fields_by_scope(Scope.content), user_id)
        # check children
        original_entry = self._get_block_from_structure(original_structure, descriptor.location.block_id)
        is_updated = is_updated or (
            descriptor.has_children and original_entry['fields']['children'] != descriptor.children
        )
        # check metadata
        if not is_updated:
            is_updated = self._compare_settings(
                descriptor.get_explicitly_set_fields_by_scope(Scope.settings),
                original_entry['fields']
            )

        # if updated, rev the structure
        if is_updated:
            new_structure = self._version_structure(original_structure, user_id)
            block_data = self._get_block_from_structure(new_structure, descriptor.location.block_id)

            block_data["definition"] = descriptor.definition_locator.definition_id
            block_data["fields"] = descriptor.get_explicitly_set_fields_by_scope(Scope.settings)
            if descriptor.has_children:
                block_data['fields']["children"] = descriptor.children

            new_id = new_structure['_id']
            block_data['edit_info'] = {
                'edited_on': datetime.datetime.now(UTC),
                'edited_by': user_id,
                'previous_version': block_data['edit_info']['update_version'],
                'update_version': new_id,
            }
            self.db_connection.insert_structure(new_structure)
            # update the index entry if appropriate
            if index_entry is not None:
                self._update_head(index_entry, descriptor.location.branch, new_id)

            # fetch and return the new item--fetching is unnecessary but a good qc step
            new_locator = BlockUsageLocator(descriptor.location)
            new_locator.version_guid = new_id
            return self.get_item(new_locator)
        else:
            # nothing changed, just return the one sent in
            return descriptor

    def persist_xblock_dag(self, xblock, user_id, force=False):
        """
        create or update the xblock and all of its children. The xblock's location must specify a course.
        If it doesn't specify a usage_id, then it's presumed to be new and need creation. This function
        descends the children performing the same operation for any that are xblocks. Any children which
        are block_ids just update the children pointer.

        All updates go into the same course version (bulk updater).

        Updates the objects which came in w/ updated location and definition_location info.

        returns the post-persisted version of the incoming xblock. Note that its children will be ids not
        objects.

        :param xblock: the head of the dag
        :param user_id: who's doing the change
        """
        # find course_index entry if applicable and structures entry
        index_entry = self._get_index_if_valid(xblock.location, force)
        structure = self._lookup_course(xblock.location)['structure']
        new_structure = self._version_structure(structure, user_id)
        new_id = new_structure['_id']
        is_updated = self._persist_subdag(xblock, user_id, new_structure['blocks'], new_id)

        if is_updated:
            self.db_connection.insert_structure(new_structure)

            # update the index entry if appropriate
            if index_entry is not None:
                self._update_head(index_entry, xblock.location.branch, new_id)

            # fetch and return the new item--fetching is unnecessary but a good qc step
            return self.get_item(
                BlockUsageLocator(
                    package_id=xblock.location.package_id,
                    block_id=xblock.location.block_id,
                    branch=xblock.location.branch,
                    version_guid=new_id
                )
            )
        else:
            return xblock

    def _persist_subdag(self, xblock, user_id, structure_blocks, new_id):
        # persist the definition if persisted != passed
        new_def_data = self._filter_special_fields(xblock.get_explicitly_set_fields_by_scope(Scope.content))
        if xblock.definition_locator is None or isinstance(xblock.definition_locator.definition_id, LocalId):
            xblock.definition_locator = self.create_definition_from_data(
                new_def_data, xblock.category, user_id)
            is_updated = True
        elif new_def_data:
            xblock.definition_locator, is_updated = self.update_definition_from_data(
                xblock.definition_locator, new_def_data, user_id)

        if isinstance(xblock.scope_ids.usage_id.block_id, LocalId):
            # generate an id
            is_new = True
            is_updated = True
            block_id = self._generate_block_id(structure_blocks, xblock.category)
            encoded_block_id = block_id
            xblock.scope_ids.usage_id.block_id = block_id
        else:
            is_new = False
            encoded_block_id = LocMapperStore.encode_key_for_mongo(xblock.location.block_id)
            is_updated = is_updated or (
                xblock.has_children and structure_blocks[encoded_block_id]['fields']['children'] != xblock.children
            )

        children = []
        if xblock.has_children:
            for child in xblock.children:
                if isinstance(child.block_id, LocalId):
                    child_block = xblock.system.get_block(child)
                    is_updated = self._persist_subdag(child_block, user_id, structure_blocks, new_id) or is_updated
                    children.append(child_block.location.block_id)
                else:
                    children.append(child)

        block_fields = xblock.get_explicitly_set_fields_by_scope(Scope.settings)
        if not is_new and not is_updated:
            is_updated = self._compare_settings(block_fields, structure_blocks[encoded_block_id]['fields'])
        if children:
            block_fields['children'] = children

        if is_updated:
            previous_version = None if is_new else structure_blocks[encoded_block_id]['edit_info'].get('update_version')
            structure_blocks[encoded_block_id] = {
                "category": xblock.category,
                "definition": xblock.definition_locator.definition_id,
                "fields": block_fields,
                'edit_info': {
                    'previous_version': previous_version,
                    'update_version': new_id,
                    'edited_by': user_id,
                    'edited_on': datetime.datetime.now(UTC)
                }
            }

        return is_updated

    def _compare_settings(self, settings, original_fields):
        """
        Return True if the settings are not == to the original fields
        :param settings:
        :param original_fields:
        """
        original_keys = original_fields.keys()
        if 'children' in original_keys:
            original_keys.remove('children')
        if len(settings) != len(original_keys):
            return True
        else:
            new_keys = settings.keys()
            for key in original_keys:
                if key not in new_keys or original_fields[key] != settings[key]:
                    return True

    def xblock_publish(self, user_id, source_course, destination_course, subtree_list, blacklist):
        """
        Publishes each xblock in subtree_list and those blocks descendants excluding blacklist
        from source_course to destination_course.

        To delete a block, publish its parent. You can blacklist the other sibs to keep them from
        being refreshed. You can also just call delete_item on the destination.

        To unpublish a block, call delete_item on the destination.

        Ensures that each subtree occurs in the same place in destination as it does in source. If any
        of the source's subtree parents are missing from destination, it raises ItemNotFound([parent_ids]).
        To determine the same relative order vis-a-vis published siblings,
        publishing may involve changing the order of previously published siblings. For example,
        if publishing `[c, d]` and source parent has children `[a, b, c, d, e]` and destination parent
        currently has children `[e, b]`, there's no obviously correct resulting order; thus, publish will
        reorder destination to `[b, c, d, e]` to make it conform with the source.

        :param source_course: a CourseLocator (can be a version or course w/ branch)

        :param destination_course: a CourseLocator which must be an existing course but branch doesn't have
        to exist yet. (The course must exist b/c Locator doesn't have everything necessary to create it).
        Note, if the branch doesn't exist, then the source_course structure's root must be in subtree_list;
        otherwise, the publish will violate the parents must exist rule.

        :param subtree_list: a list of xblock ids whose subtrees to publish. The ids can be the block
        id or BlockUsageLocator. If a Locator and if the Locator's course id != source_course, this will
        raise InvalidLocationError(locator)

        :param blacklist: a list of xblock ids to not change in the destination: i.e., don't add
        if not there, don't update if there.
        """
        # get the destination's index, and source and destination structures.
        source_structure = self._lookup_course(source_course)['structure']
        index_entry = self.db_connection.get_course_index(destination_course.package_id)
        if index_entry is None:
            # brand new course
            raise ItemNotFoundError(destination_course)
        if destination_course.branch not in index_entry['versions']:
            # must be publishing the dag root if there's no current dag
            if source_structure['root'] not in subtree_list:
                raise ItemNotFoundError(source_structure['root'])
            # create branch
            destination_structure = self._new_structure(user_id, source_structure['root'])
        else:
            destination_structure = self._lookup_course(destination_course)['structure']
            destination_structure = self._version_structure(destination_structure, user_id)

        # iterate over subtree list filtering out blacklist.
        orphans = set()
        destination_blocks = destination_structure['blocks']
        for subtree_root in subtree_list:
            # find the parents and put root in the right sequence
            parents = self._get_parents_from_structure(subtree_root, source_structure)
            if not all(parent in destination_blocks for parent in parents):
                raise ItemNotFoundError(parents)
            for parent_loc in parents:
                orphans.update(
                    self._sync_children(
                        source_structure['blocks'][parent_loc],
                        destination_blocks[parent_loc],
                        subtree_root
                ))
            # update/create the subtree and its children in destination (skipping blacklist)
            orphans.update(
                self._publish_subdag(
                    user_id, subtree_root, source_structure['blocks'], destination_blocks, blacklist or []
                )
            )
        # remove any remaining orphans
        for orphan in orphans:
            # orphans will include moved as well as deleted xblocks. Only delete the deleted ones.
            self._delete_if_true_orphan(orphan, destination_structure)

        # update the db
        self.db_connection.insert_structure(destination_structure)
        self._update_head(index_entry, destination_course.branch, destination_structure['_id'])

    def update_course_index(self, updated_index_entry):
        """
        Change the given course's index entry.

        Note, this operation can be dangerous and break running courses.

        Does not return anything useful.
        """
        self.db_connection.update_course_index(updated_index_entry)

    # TODO impl delete_all_versions
    def delete_item(self, usage_locator, user_id, delete_all_versions=False, delete_children=False, force=False):
        """
        Delete the block or tree rooted at block (if delete_children) and any references w/in the course to the block
        from a new version of the course structure.

        returns CourseLocator for new version

        raises ItemNotFoundError if the location does not exist.
        raises ValueError if usage_locator points to the structure root

        Creates a new course version. If the descriptor's location has a package_id, it moves the course head
        pointer. If the version_guid of the descriptor points to a non-head version and there's been an intervening
        change to this item, it raises a VersionConflictError unless force is True. In the force case, it forks
        the course but leaves the head pointer where it is (this change will not be in the course head).
        """
        assert isinstance(usage_locator, BlockUsageLocator) and usage_locator.is_initialized()
        original_structure = self._lookup_course(usage_locator)['structure']
        if original_structure['root'] == usage_locator.block_id:
            raise ValueError("Cannot delete the root of a course")
        index_entry = self._get_index_if_valid(usage_locator, force)
        new_structure = self._version_structure(original_structure, user_id)
        new_blocks = new_structure['blocks']
        new_id = new_structure['_id']
        parents = self.get_parent_locations(usage_locator)
        for parent in parents:
            encoded_block_id = LocMapperStore.encode_key_for_mongo(parent.block_id)
            parent_block = new_blocks[encoded_block_id]
            parent_block['fields']['children'].remove(usage_locator.block_id)
            parent_block['edit_info']['edited_on'] = datetime.datetime.now(UTC)
            parent_block['edit_info']['edited_by'] = user_id
            parent_block['edit_info']['previous_version'] = parent_block['edit_info']['update_version']
            parent_block['edit_info']['update_version'] = new_id

        def remove_subtree(block_id):
            """
            Remove the subtree rooted at block_id
            """
            encoded_block_id = LocMapperStore.encode_key_for_mongo(block_id)
            for child in new_blocks[encoded_block_id]['fields'].get('children', []):
                remove_subtree(child)
            del new_blocks[encoded_block_id]
        if delete_children:
            remove_subtree(usage_locator.block_id)
        else:
            del new_blocks[LocMapperStore.encode_key_for_mongo(usage_locator.block_id)]

        # update index if appropriate and structures
        self.db_connection.insert_structure(new_structure)

        result = CourseLocator(version_guid=new_id)

        # update the index entry if appropriate
        if index_entry is not None:
            self._update_head(index_entry, usage_locator.branch, new_id)
            result.package_id = usage_locator.package_id
            result.branch = usage_locator.branch

        return result

    def delete_course(self, package_id):
        """
        Remove the given course from the course index.

        Only removes the course from the index. The data remains. You can use create_course
        with a versions hash to restore the course; however, the edited_on and
        edited_by won't reflect the originals, of course.

        :param package_id: uses package_id rather than locator to emphasize its global effect
        """
        index = self.db_connection.get_course_index(package_id)
        if index is None:
            raise ItemNotFoundError(package_id)
        # this is the only real delete in the system. should it do something else?
        log.info("deleting course from split-mongo: %s", package_id)
        self.db_connection.delete_course_index(index['_id'])

    def get_errored_courses(self):
        """
        This function doesn't make sense for the mongo modulestore, as structures
        are loaded on demand, rather than up front
        """
        return {}

    def inherit_settings(self, block_map, block_json, inheriting_settings=None):
        """
        Updates block_json with any inheritable setting set by an ancestor and recurses to children.
        """
        if block_json is None:
            return

        if inheriting_settings is None:
            inheriting_settings = {}

        # the currently passed down values take precedence over any previously cached ones
        # NOTE: this should show the values which all fields would have if inherited: i.e.,
        # not set to the locally defined value but to value set by nearest ancestor who sets it
        # ALSO NOTE: no xblock should ever define a _inherited_settings field as it will collide w/ this logic.
        block_json.setdefault('_inherited_settings', {}).update(inheriting_settings)

        # update the inheriting w/ what should pass to children
        inheriting_settings = block_json['_inherited_settings'].copy()
        block_fields = block_json['fields']
        for field_name in inheritance.InheritanceMixin.fields:
            if field_name in block_fields:
                inheriting_settings[field_name] = block_fields[field_name]

        for child in block_fields.get('children', []):
            try:
                child = LocMapperStore.encode_key_for_mongo(child)
                self.inherit_settings(block_map, block_map[child], inheriting_settings)
            except KeyError:
                # here's where we need logic for looking up in other structures when we allow cross pointers
                # but it's also getting this during course creation if creating top down w/ children set or
                # migration where the old mongo published had pointers to privates
                pass

    def descendants(self, block_map, block_id, depth, descendent_map):
        """
        adds block and its descendants out to depth to descendent_map
        Depth specifies the number of levels of descendants to return
        (0 => this usage only, 1 => this usage and its children, etc...)
        A depth of None returns all descendants
        """
        encoded_block_id = LocMapperStore.encode_key_for_mongo(block_id)
        if encoded_block_id not in block_map:
            return descendent_map

        if block_id not in descendent_map:
            descendent_map[block_id] = block_map[encoded_block_id]

        if depth is None or depth > 0:
            depth = depth - 1 if depth is not None else None
            for child in descendent_map[block_id]['fields'].get('children', []):
                descendent_map = self.descendants(block_map, child, depth,
                    descendent_map)

        return descendent_map

    def definition_locator(self, definition):
        '''
        Pull the id out of the definition w/ correct semantics for its
        representation
        '''
        if isinstance(definition, DefinitionLazyLoader):
            return definition.definition_locator
        elif '_id' not in definition:
            return DefinitionLocator(LocalId())
        else:
            return DefinitionLocator(definition['_id'])

    def get_modulestore_type(self, course_id):
        """
        Returns an enumeration-like type reflecting the type of this modulestore
        The return can be one of:
        "xml" (for XML based courses),
        "mongo" for old-style MongoDB backed courses,
        "split" for new-style split MongoDB backed courses.
        """
        return SPLIT_MONGO_MODULESTORE_TYPE

    def internal_clean_children(self, course_locator):
        """
        Only intended for rather low level methods to use. Goes through the children attrs of
        each block removing any whose block_id is not a member of the course. Does not generate
        a new version of the course but overwrites the existing one.

        :param course_locator: the course to clean
        """
        original_structure = self._lookup_course(course_locator)['structure']
        for block in original_structure['blocks'].itervalues():
            if 'fields' in block and 'children' in block['fields']:
                block['fields']["children"] = [
                    block_id for block_id in block['fields']["children"]
                    if LocMapperStore.encode_key_for_mongo(block_id) in original_structure['blocks']
                ]
        self.db_connection.update_structure(original_structure)
        # clear cache again b/c inheritance may be wrong over orphans
        self._clear_cache(original_structure['_id'])

    def _block_matches(self, value, qualifiers):
        '''
        Return True or False depending on whether the value (block contents)
        matches the qualifiers as per get_items
        :param value:
        :param qualifiers:
        '''
        for key, criteria in qualifiers.iteritems():
            if key in value:
                target = value[key]
                if not self._value_matches(target, criteria):
                    return False
            elif criteria is not None:
                return False
        return True

    def _value_matches(self, target, criteria):
        ''' helper for _block_matches '''
        if isinstance(target, list):
            return any(self._value_matches(ele, criteria)
                for ele in target)
        elif isinstance(criteria, dict):
            if '$regex' in criteria:
                return re.search(criteria['$regex'], target) is not None
            elif not isinstance(target, dict):
                return False
            else:
                return (isinstance(target, dict) and
                        self._block_matches(target, criteria))
        else:
            return criteria == target

    def _get_index_if_valid(self, locator, force=False, continue_version=False):
        """
        If the locator identifies a course and points to its draft (or plausibly its draft),
        then return the index entry.

        raises VersionConflictError if not the right version

        :param locator: a courselocator
        :param force: if false, raises VersionConflictError if the current head of the course != the one identified
        by locator. Cannot be True if continue_version is True
        :param continue_version: if True, assumes this operation requires a head version and will not create a new
        version but instead continue an existing transaction on this version. This flag cannot be True if force is True.
        """
        if locator.package_id is None or locator.branch is None:
            if continue_version:
                raise InsufficientSpecificationError(
                    "To continue a version, the locator must point to one ({}).".format(locator)
                )
            else:
                return None
        else:
            index_entry = self.db_connection.get_course_index(locator.package_id)
            is_head = (
                locator.version_guid is None or
                index_entry['versions'][locator.branch] == locator.version_guid
            )
            if (is_head or (force and not continue_version)):
                return index_entry
            else:
                raise VersionConflictError(
                    locator,
                    index_entry['versions'][locator.branch]
                )

    def _version_structure(self, structure, user_id):
        """
        Copy the structure and update the history info (edited_by, edited_on, previous_version)
        :param structure:
        :param user_id:
        """
        new_structure = copy.deepcopy(structure)
        new_structure['_id'] = ObjectId()
        new_structure['previous_version'] = structure['_id']
        new_structure['edited_by'] = user_id
        new_structure['edited_on'] = datetime.datetime.now(UTC)
        return new_structure

    def _find_local_root(self, element_to_find, possibility, tree):
        if possibility not in tree:
            return False
        if element_to_find in tree[possibility]:
            return True
        for subtree in tree[possibility]:
            if self._find_local_root(element_to_find, subtree, tree):
                return True
        return False


    def _update_head(self, index_entry, branch, new_id):
        """
        Update the active index for the given course's branch to point to new_id

        :param index_entry:
        :param course_locator:
        :param new_id:
        """
        index_entry['versions'][branch] = new_id
        self.db_connection.update_course_index(index_entry)

    def _partition_fields_by_scope(self, category, fields):
        """
        Return dictionary of {scope: {field1: val, ..}..} for the fields of this potential xblock

        :param category: the xblock category
        :param fields: the dictionary of {fieldname: value}
        """
        if fields is None:
            return {}
        cls = self.mixologist.mix(XBlock.load_class(category, select=prefer_xmodules))
        result = collections.defaultdict(dict)
        for field_name, value in fields.iteritems():
            field = getattr(cls, field_name)
            result[field.scope][field_name] = value
        return result

    def _filter_special_fields(self, fields):
        """
        Remove any fields which split or its kvs computes or adds but does not want persisted.

        :param fields: a dict of fields
        """
        if 'location' in fields:
            del fields['location']
        if 'category' in fields:
            del fields['category']
        return fields

    def _new_structure(self, user_id, root_block_id,
                       root_category=None, block_fields=None, definition_id=None):
        """
        Internal function: create a structure element with no previous version. Must provide the root id
        but not necessarily the info needed to create it (for the use case of publishing). If providing
        root_category, must also provide block_fields and definition_id
        """
        new_id = ObjectId()
        if root_category is not None:
            encoded_root = LocMapperStore.encode_key_for_mongo(root_block_id)
            blocks = {
                encoded_root: self._new_block(
                    user_id, root_category, block_fields, definition_id, new_id
                )
            }
        else:
            blocks = {}
        return {
            '_id': new_id,
            'root': root_block_id,
            'previous_version': None,
            'original_version': new_id,
            'edited_by': user_id,
            'edited_on': datetime.datetime.now(UTC),
            'blocks': blocks
        }

    def _get_parents_from_structure(self, block_id, structure):
        """
        Given a structure, find all of block_id's parents in that structure. Note returns
        the encoded format for parent
        """
        items = []
        for parent_id, value in structure['blocks'].iteritems():
            for child_id in value['fields'].get('children', []):
                if block_id == child_id:
                    items.append(parent_id)

        return items

    def _sync_children(self, source_parent, destination_parent, new_child):
        """
        Reorder destination's children to the same as source's and remove any no longer in source.
        Return the removed ones as orphans (a set).
        """
        destination_reordered = []
        destination_children = destination_parent['fields']['children']
        source_children = source_parent['fields']['children']
        orphans = set()
        for child in destination_children:
            try:
                source_children.index(child)
            except ValueError:
                orphans.add(child)
        for child in source_children:
            if child == new_child or child in destination_children:
                destination_reordered.append(child)
        destination_parent['fields']['children'] = destination_reordered
        return orphans

    def _publish_subdag(self, user_id, block_id, source_blocks, destination_blocks, blacklist):
        """
        Update destination_blocks for the sub-dag rooted at block_id to be like the one in
        source_blocks excluding blacklist.

        Return any newly discovered orphans (as a set)
        """
        orphans = set()
        encoded_block_id = LocMapperStore.encode_key_for_mongo(block_id)
        destination_block = destination_blocks.get(encoded_block_id)
        new_block = source_blocks[encoded_block_id]
        if destination_block:
            if destination_block['edit_info']['update_version'] != new_block['edit_info']['update_version']:
                source_children = new_block['fields']['children']
                for child in destination_block['fields']['children']:
                    try:
                        source_children.index(child)
                    except ValueError:
                        orphans.add(child)
                previous_version = new_block['edit_info']['update_version']
                destination_block = copy.deepcopy(new_block)
                destination_block['fields'] = self._filter_blacklist(destination_block['fields'], blacklist)
                destination_block['edit_info']['previous_version'] = previous_version
                destination_block['edit_info']['edited_by'] = user_id
        else:
            destination_block = self._new_block(
                user_id, new_block['category'],
                self._filter_blacklist(copy.copy(new_block['fields']), blacklist),
                new_block['definition'],
                new_block['edit_info']['update_version']
            )
        for child in destination_block['fields'].get('children', []):
            if child not in blacklist:
                orphans.update(self._publish_subdag(user_id, child, source_blocks, destination_blocks, blacklist))
        destination_blocks[encoded_block_id] = destination_block
        return orphans

    def _filter_blacklist(self, fields, blacklist):
        """
        Filter out blacklist from the children field in fields. Will construct a new list for children;
        so, no need to worry about copying the children field, but it will modify fiels.
        """
        fields['children'] = [child for child in fields.get('children', []) if child not in blacklist]
        return fields

    def _delete_if_true_orphan(self, orphan, structure):
        """
        Delete the orphan and any of its descendants which no longer have parents.
        """
        if not self._get_parents_from_structure(orphan, structure):
            encoded_block_id = LocMapperStore.encode_key_for_mongo(orphan)
            for child in structure['blocks'][encoded_block_id]['fields'].get('children', []):
                self._delete_if_true_orphan(child, structure)
            del structure['blocks'][encoded_block_id]

    def _new_block(self, user_id, category, block_fields, definition_id, new_id):
        return {
            'category': category,
            'definition': definition_id,
            'fields': block_fields,
            'edit_info': {
                'edited_on': datetime.datetime.now(UTC),
                'edited_by': user_id,
                'previous_version': None,
                'update_version': new_id
            }
        }

    def _get_block_from_structure(self, structure, block_id):
        """
        Encodes the block id before retrieving it from the structure to ensure it can
        be a json dict key.
        """
        return structure['blocks'].get(LocMapperStore.encode_key_for_mongo(block_id))

    def _update_block_in_structure(self, structure, block_id, content):
        """
        Encodes the block id before accessing it in the structure to ensure it can
        be a json dict key.
        """
        structure['blocks'][LocMapperStore.encode_key_for_mongo(block_id)] = content
