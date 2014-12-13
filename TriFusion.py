#!/usr/bin/env python3
# -*- coding: utf-8 -*-#
#
#
#  Copyright 2012 Unknown <diogo@arch>
#
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
#  MA 02110-1301, USA.
#
#  Author: Diogo N. Silva
#  Version:
#  Last update:
#
from kivy.app import App
from kivy.uix.togglebutton import ToggleButton
from kivy.animation import Animation
from kivy.lang import Builder
from kivy.properties import NumericProperty, StringProperty, BooleanProperty,\
    ListProperty
from kivy.uix.screenmanager import Screen
from os.path import dirname, join


class ShowcaseScreen(Screen):
    fullscreen = BooleanProperty(False)

    def add_widget(self, *args):
        if 'content' in self.ids:
            return self.ids.content.add_widget(*args)
        return super(ShowcaseScreen, self).add_widget(*args)


class TriFusionApp(App):

    # Setting Boolean controlling the toggling of main headers
    show_options = BooleanProperty(False)
    show_side_panel = BooleanProperty(False)

    # Variable containing screen names
    screen_names = ListProperty([])

    # Getting current directory to fetch the screen kv files
    cur_dir = dirname(__file__)

    # Setting the list of input files variable
    file_list = ListProperty([])
    file_path_list = ListProperty([])

    # Current screen
    current_screen = StringProperty()
    previous_screen = StringProperty()

    # The input files already specified will be stored in this variable,
    # which will be needed in order to prevent duplication of files.
    # However, this is a temporary hack because I should be able to get
    # the list of id's from the stacklayout. In the meantime, this will
    # produce the same result
    available_files = ListProperty([])

    # Attribute to load screens
    index = NumericProperty(-1)

    def build(self):

        # Setting main window title
        self.title = "TriFusion - Streamline phylogenomics"

        # Setting available screens
        self.available_screens = ["main", "Orthology", "Process",
                                  "Statistics", "fc"]
        self.screen_names = self.available_screens
        self.available_screens = [join(self.cur_dir, "data", "screens",
                                 "{}.kv".format(screen)) for screen in
                                  self.available_screens]

        self.go_screen(0)

    def go_screen(self, idx, direct="left"):
        self.index = idx
        if self.current_screen != self.screen_names[idx]:
            # Update previous screen
            self.previous_screen = self.current_screen
            # Update current screen
            self.current_screen = self.screen_names[idx]
            self.root.ids.sm.switch_to(self.load_screen(idx), direction=direct)

    def go_previous_screen(self):
        if self.previous_screen != "":
            previous_idx = self.screen_names.index(self.previous_screen)
            self.go_screen(previous_idx, "right")

    def load(self, selection):

        self.file_path_list = selection
        self.file_list = [x.split("/")[-1] for x in selection]

        self.populate_input_files()

    def load_screen(self, idx):
        screen = Builder.load_file(self.available_screens[idx])
        return screen

    def main_toggle(self):
        self.show_options = not self.show_options

        if self.show_options:
            height = self.root.height * .2
        else:
            height = 0

        Animation(height=height, d=.3, t='out_quart').start(self.root.ids.sv)

    def side_panel_toggle(self):
        self.show_side_panel = not self.show_side_panel

        # Saving original button text to restore it

        if self.show_side_panel:
            width = self.root.width * .32
            self.root.ids.sv_but.text = "Open File(s)"
        else:
            width = 0
            self.root.ids.sv_but.text = ""

        Animation(width=width, d=.3, t="out_quart").start(self.root.ids.sp)
        # Animate the button so that the folding of the panel is smoother
        Animation(width=width * .8, d=.3, t="out_quart").start(
            self.root.ids.sv_but)

    def populate_input_files(self):

        self.root.ids.file_sl.remove_widget(self.root.ids.file_temp)

        for infile in self.file_list:

            if infile not in self.available_files:

                bt = ToggleButton(text=infile, id=infile.split(".")[0].lower(),
                                  state="down", height=self.root.height * .05)
                self.root.ids.file_sl.add_widget(bt)

                # Update available_files list
                self.available_files.append(infile)

if __name__ == '__main__':
    TriFusionApp().run()