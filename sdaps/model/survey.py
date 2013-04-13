# -*- coding: utf8 -*-
# SDAPS - Scripts for data acquisition with paper based surveys
# Copyright(C) 2008, Christoph Simon <post@christoph-simon.eu>
# Copyright(C) 2008, Benjamin Berg <benjamin@sipsolutions.net>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import bz2
import cPickle
import os
import sys
from sdaps import defs
import threading
import Queue

from sdaps import log

from sdaps.utils.ugettext import ugettext, ungettext
_ = ugettext

valid_styles = ['classic', 'code128']

class Defs(object):
    # Force a certain set of options using slots
    __slots__ = ['paper_width', 'paper_height', 'print_questionnaire_id',
                 'print_survey_id', 'style', 'duplex']

    def get_survey_id_pos(self):
        assert(self.style == 'classic')

        y_pos = self.paper_height - defs.corner_mark_bottom - defs.corner_box_padding
        y_pos -= defs.codebox_height

        left_padding = defs.corner_mark_left + 2 * defs.corner_box_padding + defs.corner_box_width
        right_padding = defs.corner_mark_right + 2 * defs.corner_box_padding + defs.corner_box_width

        text_y_pos = y_pos + defs.codebox_text_baseline_shift
        x_center = left_padding + (self.paper_width - left_padding - right_padding) / 2.0

        msb_box_x = left_padding
        lsb_box_x = self.paper_width - right_padding - defs.codebox_width

        text_x_pos = left_padding + (self.paper_width - right_padding - left_padding) / 2

        return msb_box_x, lsb_box_x, y_pos, text_x_pos, text_y_pos

    def get_questionnaire_id_pos(self):
        assert(self.style == 'classic')

        msb_box_x, lsb_box_x, y_pos, text_x_pos, text_y_pos = self.get_survey_id_pos()

        if self.print_survey_id:
            # Just move the y positions up if neccessary
            y_pos -= defs.codebox_height + defs.corner_box_padding
            text_y_pos -= defs.codebox_height + defs.corner_box_padding

        return msb_box_x, lsb_box_x, y_pos, text_x_pos, text_y_pos


class Survey(object):

    pickled_attrs = set(('sheets', 'defs', 'survey_id', 'questionnaire_ids', 'questionnaire', 'version'))

    def __init__(self):
        self.questionnaire = None
        self.sheets = list()
        self.title = unicode()
        self.info = dict()
        self.survey_id = 0
        self.global_id = None
        self.questionnaire_ids = list()
        self.version = 2
        self.defs = Defs()

        self._index = dict()
        self._index[None] = 0

    def add_questionnaire(self, questionnaire):
        self.questionnaire = questionnaire
        questionnaire.survey = self

    def add_sheet(self, sheet):
        self.sheets.append(sheet)
        sheet.survey = self
        # Select the newly added sheet
        self.index = len(self.sheets) - 1

    def calculate_survey_id(self):
        u"""Calculate the unique survey ID from the surveys settings and boxes.

        The ID only includes the boxes positions, which means that simple typo
        fixes will not change the ID most of the time."""
        import hashlib
        md5 = hashlib.new('md5')

        for qobject in self.questionnaire.qobjects:
            qobject.calculate_survey_id(md5)

        for defs_slot in self.defs.__slots__:
            if isinstance(self.defs.__getattribute__(defs_slot), float):
                md5.update(str(round(self.defs.__getattribute__(defs_slot), 1)))
            else:
                md5.update(str(self.defs.__getattribute__(defs_slot)))

        self.survey_id = 0
        # This compresses the md5 hash to a 32 bit unsigned value, by
        # taking the lower two bits of each byte.
        for i, c in enumerate(md5.digest()):
            self.survey_id += bool(ord(c) & 1) << (2 * i)
            self.survey_id += bool(ord(c) & 2) << (2 * i + 1)

    @staticmethod
    def load(survey_dir):
        import ConfigParser
        file = bz2.BZ2File(os.path.join(survey_dir, 'survey'), 'r')
        survey = cPickle.load(file)
        file.close()
        survey.survey_dir = survey_dir

        config = ConfigParser.SafeConfigParser()
        config.optionxform = str
        config.read(os.path.join(survey_dir, 'info'))
        survey.title = config.get('sdaps', 'title').decode('utf-8')

        survey.global_id = config.get('sdaps', 'global_id').decode('utf-8')
        if survey.global_id == '' or survey.global_id == 'None':
            survey.global_id = None

        survey.info = dict()
        for key, value in config.items('info'):
            survey.info[key.decode('utf-8')] = value.decode('utf-8')

        # Early versions of SDAPS 1.0 did not have the file version number
        if not hasattr(survey, 'version'):
            survey.version = 1

        survey._index = dict()
        survey._index[None] = 0

        # Run any upgrade routine (if necessary)
        survey.upgrade()

        survey.questionnaire.reinit_state()
        for sheet in survey.sheets:
            sheet.reinit_state()

        return survey

    @staticmethod
    def new(survey_dir):
        survey = Survey()
        survey.survey_dir = survey_dir
        return survey

    def save(self):
        import ConfigParser
        file = bz2.BZ2File(os.path.join(self.survey_dir, 'survey'), 'w')
        cPickle.dump(self, file, 2)
        file.close()

        # Hack to include comments. Set allow_no_value here, and add keys
        # with a '#' in the front and no value.
        config = ConfigParser.SafeConfigParser(allow_no_value=True)
            
        config.optionxform = str
        config.add_section('sdaps')
        config.add_section('info')
        config.add_section('defs')
        config.add_section('questionnaire')
        config.set('sdaps', 'title', self.title.encode('utf-8'))
        if self.global_id is not None:
            config.set('sdaps', 'global_id', self.global_id.encode('utf-8'))
        else:
            config.set('sdaps', 'global_id', '')

        for key, value in self.info.iteritems():
            config.set('info', key.encode('utf-8'), value.encode('utf-8'))

        config.set('defs', '# These values are not read back, they exist for information only!')
        for attr in self.defs.__slots__:
            config.set('defs', attr, str(getattr(self.defs, attr)).encode('utf-8'))

        config.set('questionnaire', '# These values are not read back, they exist for information only!')
        config.set('questionnaire', 'page_count', str(self.questionnaire.page_count))
        # Put the survey ID into "questionnaire". This seems sane even though
        # it is not stored there internally..
        config.set('questionnaire', 'survey_id', str(self.survey_id))

        config.write(open(os.path.join(self.survey_dir, 'info'), 'w'))

    def path(self, *path):
        return os.path.join(self.survey_dir, *path)

    def new_path(self, path):
        content = os.listdir(self.path())
        i = 1
        while path % i in content:
            i += 1
        return os.path.join(self.survey_dir, path % i)

    def get_sheet(self):
        return self.sheets[self.index]

    sheet = property(get_sheet)

    def create_thread_index(self):
        # Get the current index (from the global identifier)
        index = self.index

        # And set it
        thread = threading.current_thread()
        self._index[thread] = index

    def get_index(self):
        thread = threading.current_thread()
        if thread in self._index:
            return self._index[thread]
        else:
            return self._index[None]

    def set_index(self, value):
        thread = threading.current_thread()
        if thread in self._index:
            self._index[thread] = value
        else:
            self._index[None] = value

    index = property(get_index, set_index)

    def iterate(self, function, filter=lambda: True, *args, **kwargs):
        '''call function once for each sheet
        '''
        for self.index in range(len(self.sheets)):
            if filter():
                function(*args, **kwargs)

    def iterate_progressbar(self, function, filter=lambda: True):
        '''call function once for each sheet and display a progressbar
        '''
        print ungettext('%i sheet', '%i sheets', len(self.sheets)) % len(self.sheets)
        if len(self.sheets) == 0:
            return

        log.progressbar.start(len(self.sheets))

        for self.index in range(len(self.sheets)):
            if filter():
                function()
            log.progressbar.update(self.index + 1)

        print _('%f seconds per sheet') % (
            float(log.progressbar.elapsed_time) /
            float(log.progressbar.max_value)
        )

    def iterate_progressbar_threaded(self, function, parallel=4, filter=lambda: True, *args, **kwargs):
        print ungettext('%i sheet', '%i sheets', len(self.sheets)) % len(self.sheets)

        if len(self.sheets) == 0:
            return

        log.progressbar.start(len(self.sheets))

        queue = Queue.Queue()
        self._iterations_done = 0
        progress_lock = threading.Lock()

        def threadfunc():
            self.create_thread_index()
            item = queue.get()

            while item != -1:
                self.index = item
                function(*args, **kwargs)

                progress_lock.acquire()
                self._iterations_done += 1
                log.progressbar.update(self._iterations_done)
                progress_lock.release()

                queue.task_done()
                item = queue.get()

            queue.task_done()

        threads = []
        for i in xrange(parallel):
            thread = threading.Thread(target=threadfunc)
            thread.start()
            threads.append(thread)

        for self.index in range(len(self.sheets)):
            if filter():
                queue.put(self.index)

        # Signal threads that they are done
        for thread in threads:
            queue.put(-1)

        # Wait for the queue to be empty (all tasks done)
        queue.join()

        print _('%f seconds per sheet') % (
            float(log.progressbar.elapsed_time) /
            float(log.progressbar.max_value)
        )

    def goto_sheet(self, sheet):
        u'''goto the specified sheet object
        '''
        self.index = self.sheets.index(sheet)

    def goto_questionnaire_id(self, questionnaire_id):
        u'''goto the sheet object specified by its questionnaire_id
        '''
        sheets = filter(
            lambda sheet: sheet.questionnaire_id == questionnaire_id,
            self.sheets
        )
        if len(sheets) == 1:
            self.goto_sheet(sheets[0])
        else:
            raise ValueError

    def check_settings(self):
        u'''Do sanity checks on the different settings.'''

        if self.defs.duplex and self.questionnaire.page_count % 2 != 0:
            print _("A questionnaire that is printed in duplex needs an even amount of pages!")
            return False

        if self.defs.style == 'classic' and self.questionnaire.page_count > 6:
            print _("The 'classic' style only supports a maximum of six pages! Use the 'code128' style if you require more pages.")
            return False

        return True

    def validate_questionnaire_id(self, qid):
        if self.defs.style == "classic":
            # The ID needs to be an integer
            try:
                return int(qid)
            except ValueError:
                log.error(_("IDs need to be integers in \"classic\" style!"))
                sys.exit(1)
        elif self.defs.style == "code128":
            # Check each character for validity
            for c in unicode(qid):
                if not c in defs.c128_chars:
                    log.error(_("Invalid character %s in questionnaire ID \"%s\" in \"code128\" style!") % (c, qid))
                    sys.exit(1)
            return qid
        else:
            AssertionError()

    def __getstate__(self):
        u'''Only pickle attributes that are in the pickled_attrs set.
        '''
        dict = self.__dict__.copy()
        keys = dict.keys()
        for key in keys:
            if not key in self.pickled_attrs:
                del dict[key]
        return dict

    def upgrade(self):
        msg = _('Running upgrade routines for file format version %i')
        if self.version < 2:
            log.warn(msg % (1))
            # Changes between version 1 and 2:
            #  * Simplex surveys get a dummy page added for every image. This
            #    way they can be handled in the same way as "duplex" mode
            #    (and duplex scan can be supported).
            #  * The data for "Textbox" has a string. This will be used in the
            #    report if it contains data.

            # Insert dummy images.
            if not self.defs.duplex:
                from sdaps.model.sheet import Image

                for sheet in self.sheets:
                    images = sheet.images

                    # And readd with 
                    sheet.images = list()
                    for img in images:
                        sheet.add_image(img)

                        dummy = Image()
                        dummy.filename = "DUMMY"
                        dummy.tiff_page = -1
                        dummy.ignored = True

                        sheet.add_image(dummy)

            # Add the "text" attribute to Textbox.
            from sdaps.model.data import Textbox
            for sheet in self.sheets:
                for data in sheet.data.itervalues():
                    if isinstance(data, Textbox):
                        data.text = unicode()

        self.version = 2

