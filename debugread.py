#!/usr/bin/env python

import gtk
import re

colmap = {
        (1, 1): "#ff0000",
        (1, 6): "#cc0000",
        (2, 1): "#00ff00",
        (3, 1): "#ffff00",
        (4, 1): "#5555ff",
        (5, 1): "#ff00ff",
        (5, 6): "#cc00cc",
        (6, 1): "#00ffff",
        (6, 6): "#00cccc",
        }

class LogReader(object):

    def __init__(self, logfile):
        self.window = window = gtk.Window(gtk.WINDOW_TOPLEVEL)

        window.connect("delete_event", self.delete_event)
        window.connect("destroy", self.destroy)

        window.set_default_size(1000, 700)

        self.textview = textview = gtk.TextView(buffer=None)
        textview.set_editable(False)
        textview.modify_base(gtk.STATE_NORMAL, gtk.gdk.color_parse("#000000"))
        textview.modify_text(gtk.STATE_NORMAL, gtk.gdk.color_parse("#ffffff"))

        self.textbuffer = textbuffer = textview.get_buffer()

        coltagmap = {}

        for c, g in colmap.iteritems():
            tag = textbuffer.create_tag(foreground=g)
            coltagmap[c] = tag

        self.invisibletag = textbuffer.create_tag(invisible=True)

        colorre = re.compile(r'\x1b\x5b[^m]*m')
        valsre = re.compile(r'\x1b\x5b([0-9]+)(?:;([0-9]+))*m')
        with open(logfile, 'r') as f:
            text = f.read()

        start = 0
        nexttag = None
        it = textbuffer.get_iter_at_offset(0)
        for m in colorre.finditer(text):
            if nexttag:
                textbuffer.insert_with_tags(it, text[start:m.start()], nexttag)
            else:
                textbuffer.insert(it, text[start:m.start()])
            mm = valsre.match(m.group(0))
            mainattr = int(mm.group(1))
            if mainattr >= 30:
                assert mainattr <= 37
                col1 = mainattr - 30
                col2 = int(mm.group(2)) if mm.group(1) else 0
                nexttag = coltagmap[(col1, col2)]
            else:
                assert mainattr == 0
                nexttag = None
            start = m.end()

        del text

        sw = gtk.ScrolledWindow()
        sw.add(textview)

        self.filterentry = filterentry = gtk.Entry()

        filterentry.connect("key-press-event", self.filter_key_press_event)

        vbox = gtk.VBox()
        vbox.pack_start(filterentry, expand=False)
        vbox.pack_start(sw)

        window.add(vbox)

        textview.show()
        sw.show()
        filterentry.show()
        vbox.show()
        window.show()


    def destroy(self, widget, data=None):
        gtk.main_quit()


    def delete_event(self, widget, event, data=None):
        return False

    def filter_key_press_event(self, widget, event):
        textbuffer = self.textbuffer
        regexpstr = self.filterentry.get_text()
        if event.keyval != 65293:
            return
        if not regexpstr:
            print "CLEAR FILTER"
            textbuffer.remove_tag(self.invisibletag,
                    textbuffer.get_start_iter(), textbuffer.get_end_iter())
            return
        print "UPDATE FILTER", regexpstr
        regexp = re.compile(regexpstr)
        curstartit = textbuffer.get_start_iter()
        startit = curstartit
        lastvisible = True
        for i in range(0, textbuffer.get_line_count()):
            curendit = textbuffer.get_iter_at_line(i)
            text = curstartit.get_text(curendit)
            if regexp.search(text):
                if not lastvisible:
                    textbuffer.apply_tag(self.invisibletag, startit, curstartit)
                    lastvisible = True
                    startit = curstartit
            else:
                if lastvisible:
                    textbuffer.remove_tag(self.invisibletag, startit, curstartit)
                    lastvisible = False
                    startit = curstartit
            curstartit = curendit

        if lastvisible:
            textbuffer.remove_tag(self.invisibletag, startit, textbuffer.get_end_iter())
        else:
            textbuffer.apply_tag(self.invisibletag, startit, textbuffer.get_end_iter())

    def main(self):
        gtk.main()


if __name__ == "__main__":
    import sys
    logreader = LogReader(sys.argv[1])
    logreader.main()
