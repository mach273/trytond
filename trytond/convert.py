#This file is part of Tryton.  The COPYRIGHT file at the top level of
#this repository contains the full copyright notices and license terms.
import time
from xml import sax
import logging
import traceback
import sys
import re
from sql import Table
from itertools import izip
from collections import defaultdict

from .version import VERSION
from .tools import safe_eval, grouped_slice
from .transaction import Transaction

CDATA_START = re.compile('^\s*\<\!\[cdata\[', re.IGNORECASE)
CDATA_END = re.compile('\]\]\>\s*$', re.IGNORECASE)


class DummyTagHandler:
    """Dubhandler implementing empty methods. Will be used when whe
    want to ignore the xml content"""

    def __init__(self):
        pass

    def startElement(self, name, attributes):
        pass

    def characters(self, data):
        pass

    def endElement(self, name):
        pass


class MenuitemTagHandler:
    """Taghandler for the tag <record> """
    def __init__(self, master_handler):
        self.mh = master_handler
        self.xml_id = None

    def startElement(self, name, attributes):
        cursor = Transaction().cursor

        values = {}

        self.xml_id = attributes['id']

        for attr in ('name', 'icon', 'sequence', 'parent', 'action', 'groups'):
            if attributes.get(attr):
                values[attr] = attributes.get(attr)

        if attributes.get('active'):
            values['active'] = bool(safe_eval(attributes['active']))

        if values.get('parent'):
            values['parent'] = self.mh.get_id(values['parent'])

        action_name = None
        if values.get('action'):
            action_id = self.mh.get_id(values['action'])

            # TODO maybe use a prefetch for this:
            action = Table('ir_action')
            report = Table('ir_action_report')
            act_window = Table('ir_action_act_window')
            wizard = Table('ir_action_wizard')
            url = Table('ir_action_url')
            act_window_view = Table('ir_action_act_window_view')
            view = Table('ir_ui_view')
            icon = Table('ir_ui_icon')
            cursor.execute(*action.join(
                    report, 'LEFT',
                    condition=action.id == report.action
                    ).join(act_window, 'LEFT',
                    condition=action.id == act_window.action
                    ).join(wizard, 'LEFT',
                    condition=action.id == wizard.action
                    ).join(url, 'LEFT',
                    condition=action.id == url.action
                    ).join(act_window_view, 'LEFT',
                    condition=act_window.id == act_window_view.act_window
                    ).join(view, 'LEFT',
                    condition=view.id == act_window_view.view
                    ).join(icon, 'LEFT',
                    condition=action.icon == icon.id).select(
                    action.name, action.type,
                    view.type, view.field_childs,
                    icon.name,
                    where=(report.id == action_id)
                    | (act_window.id == action_id)
                    | (wizard.id == action_id)
                    | (url.id == action_id),
                    order_by=act_window_view.sequence, limit=1))
            action_name, action_type, view_type, field_childs, icon_name = \
                cursor.fetchone()

            values['action'] = '%s,%s' % (action_type, action_id)

            icon = attributes.get('icon', '')
            if icon:
                values['icon'] = icon
            elif icon_name:
                values['icon'] = icon_name
            elif action_type == 'ir.action.wizard':
                values['icon'] = 'tryton-executable'
            elif action_type == 'ir.action.report':
                values['icon'] = 'tryton-print'
            elif action_type == 'ir.action.act_window':
                if view_type == 'tree':
                    if field_childs:
                        values['icon'] = 'tryton-tree'
                    else:
                        values['icon'] = 'tryton-list'
                elif view_type == 'form':
                    values['icon'] = 'tryton-new'
                elif view_type == 'graph':
                    values['icon'] = 'tryton-graph'
                elif view_type == 'calendar':
                    values['icon'] = 'tryton-calendar'
            elif action_type == 'ir.action.url':
                values['icon'] = 'tryton-web-browser'
            else:
                values['icon'] = 'tryton-new'

        if values.get('groups'):
            raise Exception("Please use separate records for groups")

        if not values.get('name'):
            if not action_name:
                raise Exception("Please provide at least a 'name' attributes "
                        "or a 'action' attributes on the menuitem tags.")
            else:
                values['name'] = action_name

        if values.get('sequence'):
            values['sequence'] = int(values['sequence'])

        self.values = values

    def characters(self, data):
        pass

    def endElement(self, name):
        """Must return the object to use for the next call """
        if name != "menuitem":
            return self
        else:
            self.mh.import_record('ir.ui.menu', self.values, self.xml_id)
            return None

    def current_state(self):
        return "Tag menuitem with id: %s" % self.xml_id


class RecordTagHandler:

    """Taghandler for the tag <record> and all the tags inside it"""

    def __init__(self, master_handler):
        # Remind reference of parent handler
        self.mh = master_handler
        # stock xml_id parsed in one module
        self.xml_ids = []
        self.model = None
        self.xml_id = None
        self.update = None
        self.values = None
        self.current_field = None
        self.cdata = None
        self.start_cdata = None

    def startElement(self, name, attributes):

        # Manage the top level tag
        if name == "record":
            self.model = self.mh.pool.get(attributes["model"])
            assert self.model, ("The model %s does not exist !"
                % (attributes["model"],))

            self.xml_id = attributes["id"]
            self.update = bool(int(attributes.get('update', '0')))

            # create/update a dict containing fields values
            self.values = {}

            self.current_field = None
            self.cdata = False

            return self.xml_id

        # Manage included tags:
        elif name == "field":

            field_name = attributes['name']
            field_type = attributes.get('type', '')
            # Create a new entry in the values
            self.values[field_name] = ""
            # Remind the current name (see characters)
            self.current_field = field_name
            # Put a flag to escape cdata tags
            if field_type == "xml":
                self.cdata = "start"

            # Catch the known attributes
            search_attr = attributes.get('search', '')
            ref_attr = attributes.get('ref', '')
            eval_attr = attributes.get('eval', '')

            if search_attr:
                search_model = self.model._fields[field_name].model_name
                SearchModel = self.mh.pool.get(search_model)
                with Transaction().set_context(active_test=False):
                    found, = SearchModel.search(safe_eval(search_attr))
                    self.values[field_name] = found.id

            elif ref_attr:
                self.values[field_name] = self.mh.get_id(ref_attr)

            elif eval_attr:
                context = {}
                context['time'] = time
                context['version'] = VERSION.rsplit('.', 1)[0]
                context['ref'] = self.mh.get_id
                context['obj'] = lambda *a: 1
                self.values[field_name] = safe_eval(eval_attr, context)

        else:
            raise Exception("Tags '%s' not supported inside tag record." %
                    (name,))

    def characters(self, data):

        """If whe are in a field tag, consume all the content"""

        if not self.current_field:
            return
        # Escape start cdata tag if necessary
        if self.cdata == "start":
            data = CDATA_START.sub('', data)
            self.start_cdata = "inside"

        self.values[self.current_field] += data

    def endElement(self, name):

        """Must return the object to use for the next call, if name is
        not 'record' we return self to keep our hand on the
        process. If name is 'record' we return None to end the
        delegation"""

        if name == "field":
            if not self.current_field:
                raise Exception("Application error"
                                "current_field expected to be set.")
            # Escape end cdata tag :
            if self.cdata in ('inside', 'start'):
                self.values[self.current_field] = \
                    CDATA_END.sub('', self.values[self.current_field])
                self.cdata = 'done'

                value = self.values[self.current_field]
                match = re.findall('[^%]%\((.*?)\)[ds]', value)
                xml_ids = {}
                for xml_id in match:
                    xml_ids[xml_id] = self.mh.get_id(xml_id)
                self.values[self.current_field] = value % xml_ids

            self.current_field = None
            return self

        elif name == "record":
            if self.xml_id in self.xml_ids and not self.update:
                raise Exception('Duplicate id: "%s".' % (self.xml_id,))
            self.mh.import_record(
                self.model.__name__, self.values, self.xml_id)
            self.xml_ids.append(self.xml_id)
            return None
        else:
            raise Exception("Unexpected closing tag '%s'" % (name,))

    def current_state(self):
        return "In tag record: model %s with id %s." % \
               (self.model and self.model.__name__ or "?", self.xml_id)


# Custom exception:
class Unhandled_field(Exception):
    """
    Raised when a field type is not supported by the update mechanism.
    """
    pass


class Fs2bdAccessor:
    """
    Used in TrytondXmlHandler.
    Provide some helper function to ease cache access and management.
    """

    def __init__(self, ModelData, pool):
        self.fs2db = {}
        self.fetched_modules = []
        self.ModelData = ModelData
        self.browserecord = {}
        self.pool = pool

    def get(self, module, fs_id):
        if module not in self.fetched_modules:
            self.fetch_new_module(module)
        return self.fs2db[module].get(fs_id, None)

    def get_browserecord(self, module, model_name, db_id):
        if module not in self.fetched_modules:
            self.fetch_new_module(module)
        if model_name in self.browserecord[module] \
                and db_id in self.browserecord[module][model_name]:
            return self.browserecord[module][model_name][db_id]
        return None

    def set(self, module, fs_id, values):
        """
        Whe call the prefetch function here to. Like that whe are sure
        not to erase data when get is called.
        """
        if not module in self.fetched_modules:
            self.fetch_new_module(module)
        if fs_id not in self.fs2db[module]:
            self.fs2db[module][fs_id] = {}
        fs2db_val = self.fs2db[module][fs_id]
        for key, val in values.items():
            fs2db_val[key] = val

    def reset_browsercord(self, module, model_name, ids=None):
        if module not in self.fetched_modules:
            return
        self.browserecord[module].setdefault(model_name, {})
        Model = self.pool.get(model_name)
        if not ids:
            ids = self.browserecord[module][model_name].keys()
        models = Model.browse(ids)
        for model in models:
            if model.id in self.browserecord[module][model_name]:
                for cache in Transaction().cursor.cache.values():
                    for cache in (cache, cache.get('_language_cache',
                                {}).values()):
                        if (model_name in cache
                                and model.id in cache[model_name]):
                            cache[model_name][model.id] = {}
            self.browserecord[module][model_name][model.id] = model

    def fetch_new_module(self, module):
        self.fs2db[module] = {}
        module_data_ids = self.ModelData.search([
                ('module', '=', module),
                ], order=[('db_id', 'ASC')])

        record_ids = {}
        for rec in self.ModelData.browse(module_data_ids):
            self.fs2db[rec.module][rec.fs_id] = {
                "db_id": rec.db_id, "model": rec.model,
                "id": rec.id, "values": rec.values
                }
            record_ids.setdefault(rec.model, [])
            record_ids[rec.model].append(rec.db_id)

        object_name_list = set(self.pool.object_name_list())

        self.browserecord[module] = {}
        for model_name in record_ids.keys():
            if model_name not in object_name_list:
                continue
            Model = self.pool.get(model_name)
            self.browserecord[module][model_name] = {}
            for sub_record_ids in grouped_slice(record_ids[model_name]):
                with Transaction().set_context(active_test=False):
                    records = Model.search([
                        ('id', 'in', list(sub_record_ids)),
                        ], order=[('id', 'ASC')])
                with Transaction().set_context(language='en_US'):
                    models = Model.browse(map(int, records))
                for model in models:
                    self.browserecord[module][model_name][model.id] = model
        self.fetched_modules.append(module)


class TrytondXmlHandler(sax.handler.ContentHandler):

    def __init__(self, pool, module, module_state):
        "Register known taghandlers, and managed tags."
        sax.handler.ContentHandler.__init__(self)

        self.pool = pool
        self.module = module
        self.ModelData = pool.get('ir.model.data')
        self.fs2db = Fs2bdAccessor(self.ModelData, pool)
        self.to_delete = self.populate_to_delete()
        self.noupdate = None
        self.module_state = module_state
        self.grouped = None
        self.grouped_creations = defaultdict(dict)
        self.grouped_write = defaultdict(list)
        self.grouped_model_data = []
        self.skip_data = False
        Module = pool.get('ir.module.module')
        self.installed_modules = [m.name for m in Module.search([
                    ('state', 'in', ['installed', 'to upgrade']),
                    ])]

        # Tag handlders are used to delegate the processing
        self.taghandlerlist = {
            'record': RecordTagHandler(self),
            'menuitem': MenuitemTagHandler(self),
            }
        self.taghandler = None

        # Managed tags are handled by the current class
        self.managedtags = ["data", "tryton"]

        # Connect to the sax api:
        self.sax_parser = sax.make_parser()
        # Tell the parser we are not interested in XML namespaces
        self.sax_parser.setFeature(sax.handler.feature_namespaces, 0)
        self.sax_parser.setContentHandler(self)

    def parse_xmlstream(self, stream):
        """
        Take a byte stream has input and parse the xml content.
        """

        source = sax.InputSource()
        source.setByteStream(stream)

        try:
            self.sax_parser.parse(source)
        except Exception:
            logging.getLogger("convert").error(
                "Error while parsing xml file:\n" + self.current_state())
            raise
        return self.to_delete

    def startElement(self, name, attributes):
        """Rebind the current handler if necessary and call
        startElement on it"""

        if not self.taghandler:

            if name in self.taghandlerlist:
                self.taghandler = self.taghandlerlist[name]
                self.taghandler.startElement(name, attributes)

            elif name == "data":
                self.noupdate = bool(int(attributes.get("noupdate", '0')))
                self.grouped = bool(int(attributes.get('grouped', 0)))
                if self.pool.test and \
                        bool(int(attributes.get("skiptest", '0'))):
                    self.skip_data = True
                else:
                    self.skip_data = False
                depends = attributes.get('depends', '').split(',')
                depends = [m.strip() for m in depends if m]
                if depends:
                    if not all((m in self.installed_modules for m in depends)):
                        self.skip_data = True

            elif name == "tryton":
                pass

            else:
                logging.getLogger("convert").info("Tag %s not supported" %
                    name)
                return
        elif not self.skip_data:
            self.taghandler.startElement(name, attributes)

    def characters(self, data):
        if self.taghandler:
            self.taghandler.characters(data)

    def endElement(self, name):

        if name == 'data' and self.grouped:
            for model, values in self.grouped_creations.iteritems():
                self.create_records(model, values.values(), values.keys())
            self.grouped_creations.clear()
            for key, actions in self.grouped_write.iteritems():
                module, model = key
                self.write_records(module, model, *actions)
            self.grouped_write.clear()
        if name == 'data' and self.grouped_model_data:
            self.ModelData.write(*self.grouped_model_data)
            del self.grouped_model_data[:]

        # Closing tag found, if we are in a delegation the handler
        # know what to do:
        if self.taghandler and not self.skip_data:
            self.taghandler = self.taghandler.endElement(name)
        if self.taghandler == self.taghandlerlist.get(name):
            self.taghandler = None

    def current_state(self):
        if self.taghandler:
            return self.taghandler.current_state()
        else:
            return ''

    def get_id(self, xml_id):

        if '.' in xml_id:
            module, xml_id = xml_id.split('.')
        else:
            module = self.module

        if self.fs2db.get(module, xml_id) is None:
            raise Exception("Reference to %s not found"
                % ".".join([module, xml_id]))
        return self.fs2db.get(module, xml_id)["db_id"]

    @staticmethod
    def _clean_value(key, record):
        """
        Take a field name, a browse_record, and a reference to the
        corresponding object.  Return a raw value has it must look on the
        db.
        """
        Model = record.__class__
        # search the field type in the object or in a parent
        field_type = Model._fields[key]._type

        # handle the value regarding to the type
        if field_type == 'many2one':
            return getattr(record, key).id if getattr(record, key) else None
        elif field_type == 'reference':
            if not getattr(record, key):
                return None
            elif not isinstance(getattr(record, key), basestring):
                return str(getattr(record, key))
            ref_mode, ref_id = getattr(record, key).split(',', 1)
            try:
                ref_id = safe_eval(ref_id)
            except Exception:
                pass
            if isinstance(ref_id, (list, tuple)):
                ref_id = ref_id[0]
            return ref_mode + ',' + str(ref_id)
        elif field_type in ['one2many', 'many2many']:
            raise Unhandled_field("Unhandled field %s" % key)
        else:
            return getattr(record, key)

    def populate_to_delete(self):
        """Create a list of all the records that whe should met in the update
        process. The records that are not encountered are deleted from the
        database in post_import."""

        # Fetch the data in id descending order to avoid depedendcy
        # problem when the corresponding recordds will be deleted:
        module_data = self.ModelData.search([
                ('module', '=', self.module),
                ], order=[('id', 'DESC')])
        return set(rec.fs_id for rec in module_data)

    def import_record(self, model, values, fs_id):
        module = self.module

        if not fs_id:
            raise Exception('import_record : Argument fs_id is mandatory')

        if '.' in fs_id:
            assert len(fs_id.split('.')) == 2, ('"%s" contains too many dots. '
                'file system ids should contain ot most one dot ! '
                'These are used to refer to other modules data, '
                'as in module.reference_id' % (fs_id))

            module, fs_id = fs_id.split('.')
            if not self.fs2db.get(module, fs_id):
                raise Exception('Reference to %s.%s not found'
                    % (module, fs_id))

        Model = self.pool.get(model)

        if self.fs2db.get(module, fs_id):

            # Remove this record from the to_delete list. This means that
            # the corresponding record have been found.
            if module == self.module and fs_id in self.to_delete:
                self.to_delete.remove(fs_id)

            if self.noupdate and self.module_state != 'to install':
                return

            # this record is already in the db:
            # XXX maybe use only one call to get()
            db_id, db_model, mdata_id, old_values = [
                self.fs2db.get(module, fs_id)[x]
                for x in ["db_id", "model", "id", "values"]]

            if not old_values:
                old_values = {}
            else:
                old_values = self.ModelData.load_values(old_values)

            for key in old_values:
                if isinstance(old_values[key], str):
                    # Fix for migration to unicode
                    old_values[key] = old_values[key].decode('utf-8')

            if model != db_model:
                raise Exception("This record try to overwrite "
                    "data with the wrong model: %s (module: %s)"
                    % (fs_id, module))

            record = self.fs2db.get_browserecord(module, Model.__name__, db_id)
            #Re-create record if it was deleted
            if not record:
                with Transaction().set_context(
                        module=module, language='en_US'):
                    record, = Model.create([values])

                # reset_browsercord
                self.fs2db.reset_browsercord(
                    module, Model.__name__, [record.id])

                record = self.fs2db.get_browserecord(
                    module, Model.__name__, record.id)

                data = self.ModelData.search([
                    ('fs_id', '=', fs_id),
                    ('module', '=', module),
                    ('model', '=', Model.__name__),
                    ], limit=1)
                self.ModelData.write(data, {
                    'db_id': record.id,
                    })
                self.fs2db.get(module, fs_id)["db_id"] = record.id

            to_update = {}
            for key in values:

                db_field = self._clean_value(key, record)

                # if the fs value is the same has in the db, whe ignore it
                val = values[key]
                if isinstance(values[key], str):
                    # Fix for migration to unicode
                    val = values[key].decode('utf-8')
                if db_field == val:
                    continue

                # we cannot update a field if it was changed by a user...
                if key not in old_values:
                    expected_value = Model._defaults.get(key,
                        lambda *a: None)()
                else:
                    expected_value = old_values[key]

                # Migration from 2.0: Reference field change value
                field_type = Model._fields[key]._type
                if field_type == 'reference':
                    if (expected_value and expected_value.endswith(',0')
                            and not db_field):
                        db_field = expected_value

                # ... and we consider that there is an update if the
                # expected value differs from the actual value, _and_
                # if they are not false in a boolean context (ie None,
                # False, {} or [])
                if db_field != expected_value and (db_field or expected_value):
                    logging.getLogger("convert").warning(
                        "Field %s of %s@%s not updated (id: %s), because "
                        "it has changed since the last update"
                        % (key, record.id, model, fs_id))
                    continue

                # so, the field in the fs and in the db are different,
                # and no user changed the value in the db:
                to_update[key] = values[key]

            if self.grouped:
                self.grouped_write[(module, model)].extend(
                    (record, to_update, old_values, fs_id, mdata_id))
            else:
                self.write_records(module, model, record, to_update,
                    old_values, fs_id, mdata_id)
            self.grouped_model_data.extend(([self.ModelData(mdata_id)], {
                        'fs_values': self.ModelData.dump_values(values),
                        }))
        else:
            if self.grouped:
                self.grouped_creations[model][fs_id] = values
            else:
                self.create_records(model, [values], [fs_id])

    def create_records(self, model, vlist, fs_ids):
        Model = self.pool.get(model)

        with Transaction().set_context(module=self.module, language='en_US'):
            records = Model.create(vlist)

        mdata_values = []
        for record, values, fs_id in izip(records, vlist, fs_ids):
            for key in values:
                values[key] = self._clean_value(key, record)

            mdata_values.append({
                    'fs_id': fs_id,
                    'model': model,
                    'module': self.module,
                    'db_id': record.id,
                    'values': self.ModelData.dump_values(values),
                    'fs_values': self.ModelData.dump_values(values),
                    'noupdate': self.noupdate,
                    })

        models_data = self.ModelData.create(mdata_values)

        for record, values, fs_id, mdata in izip(
                records, vlist, fs_ids, models_data):
            self.fs2db.set(self.module, fs_id, {
                    'db_id': record.id,
                    'model': model,
                    'id': mdata.id,
                    'values': self.ModelData.dump_values(values),
                    })
        self.fs2db.reset_browsercord(self.module, model,
            [r.id for r in records])

    def write_records(self, module, model,
            record, values, old_values, fs_id, mdata_id, *args):
        args = (record, values, old_values, fs_id, mdata_id) + args
        Model = self.pool.get(model)

        actions = iter(args)
        to_update = []
        for record, values, _, _, _ in zip(*((actions,) * 5)):
            if values:
                to_update += [[record], values]
        # if there is values to update:
        if to_update:
            # write the values in the db:
            with Transaction().set_context(
                    module=module, language='en_US'):
                Model.write(*to_update)
            self.fs2db.reset_browsercord(
                module, Model.__name__, sum(to_update[::2], []))

        actions = iter(to_update)
        for records, values in zip(actions, actions):
            record, = records
            # re-read it: this ensure that we store the real value
            # in the model_data table:
            record = self.fs2db.get_browserecord(
                module, Model.__name__, record.id)
            if not record:
                record = Model(record.id)
            for key in values:
                values[key] = self._clean_value(key, record)

        actions = iter(args)
        for record, values, old_values, fs_id, mdata_id in zip(
                *((actions,) * 5)):
            temp_values = old_values.copy()
            temp_values.update(values)
            values = temp_values

            if values != old_values:
                self.grouped_model_data.extend(([self.ModelData(mdata_id)], {
                            'fs_id': fs_id,
                            'model': model,
                            'module': module,
                            'db_id': record.id,
                            'values': self.ModelData.dump_values(values),
                            }))

        # reset_browsercord to keep cache memory low
        self.fs2db.reset_browsercord(module, Model.__name__, args[::5])


def post_import(pool, module, to_delete):
    """
    Remove the records that are given in to_delete.
    """
    cursor = Transaction().cursor
    mdata_delete = []
    ModelData = pool.get("ir.model.data")

    with Transaction().set_context(active_test=False):
        mdata = ModelData.search([
            ('fs_id', 'in', to_delete),
            ('module', '=', module),
            ], order=[('id', 'DESC')])

    object_name_list = set(pool.object_name_list())
    for mrec in mdata:
        model, db_id = mrec.model, mrec.db_id

        logging.getLogger("convert").info(
                'Deleting %s@%s' % (db_id, model))
        try:
            # Deletion of the record
            if model in object_name_list:
                Model = pool.get(model)
                Model.delete([Model(db_id)])
                mdata_delete.append(mrec)
            else:
                logging.getLogger("convert").warning(
                        'Could not delete id %d of model %s because model no '
                        'longer exists.' % (db_id, model))
            cursor.commit()
        except Exception:
            cursor.rollback()
            tb_s = ''.join(traceback.format_exception(*sys.exc_info()))
            logging.getLogger("convert").error(
                'Could not delete id: %d of model %s\n'
                    'There should be some relation '
                    'that points to this resource\n'
                    'You should manually fix this '
                    'and restart --update=module\n'
                    'Exception: %s' %
                    (db_id, model, tb_s))
            if 'active' in Model._fields:
                Model.write([Model(db_id)], {
                        'active': False,
                        })

    # Clean model_data:
    if mdata_delete:
        ModelData.delete(mdata_delete)
        cursor.commit()

    return True
