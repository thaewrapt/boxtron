#!/usr/bin/python3

"""
DOSBox configuration file generator.
"""

import argparse
import configparser
import hashlib
import os
import re

import cuescanner
import midi

from log import log, log_err, print_err
from settings import SETTINGS as settings
from winpathlib import to_posix_path

COMMENT_SECTION = """
# Generated by Boxtron
# Based on args to Windows version of DOSBox:
# {}

""".lstrip()

SDL_SECTION_1 = """
[sdl]
fullscreen=true
fullresolution={resolution}
output=opengl
autolock=false
waitonerror=true

""".lstrip()

SDL_SECTION_2 = """
[sdl]
# fullscreen=true
# output=opengl
# autolock=false

""".lstrip()

RENDER_SECTION_1 = """
[render]
aspect={aspect}
scaler={scaler}

""".lstrip()

RENDER_SECTION_2 = """
[render]
# aspect: Do aspect correction for games using 320x200 resolution.
#         Read more: https://www.dosbox.com/wiki/Dosbox.conf#aspect
# scaler: Specifies which scaler is used to enlarge and enhance low resolution
#         modes, before any scaling done through OpenGL.
#         Read more: https://www.dosbox.com/wiki/Dosbox.conf#scaler
""".lstrip()

# The default DOSBox configuration sets `cycles=auto` which means:
#
# - real-mode games will run at 3000 cycles
# - protected mode games run with cycles=max
#
# This makes many (most?) real-mode games unplayable due to very low
# performance.  Using `max 95%` seems to help with this issue in all
# tested games so far without negatively affecting protected mode games.
#
CPU_SECTION = """
[cpu]
core=auto
cputype=auto
cycles=max 95%

""".lstrip()

SBLASTER_SECTION = """
[sblaster]
sbtype=sb16
sbbase={base}
irq={irq}
dma={dma}
hdma={hdma}

""".lstrip()

SBLASTER_INFO = """
Digital Sound: Sound Blaster 16
    Base Port: {base}
          IRQ: {irq}
          DMA: {dma}
""".strip()

MIDI_SECTION = """
[midi]
mpu401=intelligent
mididevice=default
midiconfig={port}

""".lstrip()

DOS_SECTION = """
[dos]
xms={xms}
ems={ems}
umb={umb}

""".lstrip()

# Port 330 is hard-coded in DOSBox
MIDI_INFO = """
        Music: General MIDI (MPU-401 compatible)
         Port: 330
""" [1:]

MIDI_INFO_NA = """
        Music: No MIDI synthesiser found
""" [1:]


class DosboxConfigParser(configparser.ConfigParser):
    """Specialization of ConfigParser for DOSBox format."""

    # pylint: disable=too-many-ancestors

    def __init__(self):
        super().__init__(allow_no_value=True,
                         delimiters='=',
                         strict=False,
                         interpolation=None)
        self.optionxform = str
        self.autoexec_lines = []

    def read(self, filenames, encoding=None):
        """Read and parse a filename or an iterable of filenames.

        Read ConfigParser.read documentation for details.
        """
        if filenames.__class__ is list:
            raise NotImplementedError

        assert filenames.__class__ is str

        # first pass to read everything except autoexec section
        super().read(filenames, encoding)

        # second pass to simply read lines in autoexec without standing on
        # our heads and modifying ConfigParser internals:
        with open(filenames, 'r', encoding=encoding) as txt:
            in_section = False
            for line in txt:
                if in_section:
                    self.autoexec_lines.append(line.rstrip())
                    continue
                if line.strip().startswith('[autoexec]'):
                    in_section = True

    def get_autoexec(self):
        """Return list of lines in autoexec section."""
        return self.autoexec_lines


class DosboxConfiguration(dict):
    """Class representing DOSBox configuration.

    Autoexec section represents commands from default .conf files,
    files referenced by -conf argument, commands injected with -c argument
    and commands usually generated by DOSBox itself.

    Other sections of raw configuration represent relevant sections
    found in configuration files.  Values inside sections override
    values seen in previous configuration files.
    """

    def __init__(self,
                 *,
                 commands=[],
                 conf_files=[],
                 exe=None,
                 noautoexec=False,
                 exit_after_exe=False,
                 tweak_conf={}):
        assert commands or conf_files or exe
        dict.__init__(self)
        self['autoexec'] = []
        self.raw_autoexec = self['autoexec']
        self.encoding = 'utf-8'

        for win_path in (conf_files or self.__get_default_conf__()):
            path = to_posix_path(win_path)
            conf, enc = parse_dosbox_config(path)
            self.__import_ini_sections__(conf)
            if enc != 'utf-8':
                self.encoding = enc
            if not noautoexec and conf.has_section('autoexec'):
                self.raw_autoexec.extend(conf.get_autoexec())

        self.raw_autoexec.extend(cmd for cmd in commands)

        tweak = configparser.ConfigParser()
        tweak.read_dict(tweak_conf)
        self.__import_ini_sections__(tweak)

        if exe:
            posix_path = to_posix_path(exe)
            path, file = os.path.split(posix_path)
            self.raw_autoexec.append('mount C {0}'.format(path or '.'))
            self.raw_autoexec.append('C:')
            if file.lower().endswith('.bat'):
                self.raw_autoexec.append('call {0}'.format(file))
            else:
                self.raw_autoexec.append(file)
            if exit_after_exe:
                self.raw_autoexec.append('exit')

    def __get_default_conf__(self):
        # pylint: disable=no-self-use
        path = to_posix_path('dosbox.conf')
        if path and os.path.isfile(path):
            return [path]
        return []

    def __import_ini_sections__(self, config):
        for name in config.sections():
            if name == 'autoexec':
                continue
            if not self.has_section(name):
                self[name] = config[name]
                continue
            for opt, val in config[name].items():
                self.set(name, opt, val)

    def sections(self):
        """Return a list of section names."""
        return list(self.keys())

    def has_section(self, section):
        "Indicates whether the named section is present in the configuration."
        return section in self.keys()

    def set(self, section, option, value):
        """Set option in section to value.

        If the given section exists, set the given option to the specified
        value; otherwise raise NoSectionError.
        """
        if section not in self:
            raise configparser.NoSectionError
        self[section][option] = value


def cleanup_old_conf_files(app_id, args):
    """Remove old unused, versions of .conf files."""
    old_v0 = uniq_conf_name_salted('steam_dos', app_id, args, '')
    old_v1 = uniq_conf_name_salted('steam_dos', app_id, args, 'v1')
    for name in 'steam_dos_audio.conf', 'steam_dos_auto.conf', old_v0, old_v1:
        if os.path.isfile(name):
            os.remove(name)


def uniq_conf_name(app_id, args):
    """Return unique .conf file name for given SteamAppId and arguments."""
    return uniq_conf_name_salted('boxtron', app_id, args, 'v2')


def uniq_conf_name_salted(pfx, app_id, args, salt):
    """Implements .conf name generator."""
    uid_line = app_id + ''.join(args) + salt
    uid = hashlib.sha1(uid_line.encode('utf-8')).hexdigest()[:6]
    return '{0}_{1}_{2}.conf'.format(pfx, app_id, uid)


def parse_dosbox_config(conf_file):
    """Parse DOSBox configuration file."""
    assert conf_file
    config = DosboxConfigParser()
    encoding = 'utf-8'
    try:
        # Try simply reading a .conf file, assuming it's utf-8 encoded,
        # as any modern text editor will likely create utf-8 file by
        # default.
        #
        config.read(conf_file)

    except UnicodeDecodeError:
        # Failed decoding from utf-8 means, that likely there are some
        # graphical glyphs in autoexec section of a .conf file.
        #
        # This seems to be a common case for .conf files distributed
        # with GOG games. Just retry with specific old encoding.
        #
        encoding = 'cp1250'
        config.read(conf_file, encoding=encoding)

    return config, encoding


def convert_cue_file(path):
    """Handle case-sensitive paths inside .cue files."""
    if not cuescanner.is_cue_file(path):
        return path
    if cuescanner.valid_cue_file_paths(path):
        return path
    cuescanner.create_fixed_cue_file(path, 'boxtron.cue')
    return 'boxtron.cue'


def to_linux_autoexec(autoexec):
    """Convert case-sensitive parts in autoexec."""
    cmd_1 = r'@? *(mount|imgmount) +([a-z]):? +"([^"]+)"( +(.*))?'
    cmd_2 = r'@? *(mount|imgmount) +([a-z]):? +([^ ]+)( +(.*))?'
    mount_cmd_1 = re.compile(cmd_1, re.IGNORECASE)
    mount_cmd_2 = re.compile(cmd_2, re.IGNORECASE)
    change_drv = re.compile(r'@? *([a-z]:)\\? *$', re.IGNORECASE)
    for line in autoexec:
        match = mount_cmd_1.match(line) or mount_cmd_2.match(line)
        if match:
            cmd = match.group(1).lower()
            drive = match.group(2).upper()
            path = to_posix_path(match.group(3))
            if cmd == 'imgmount':
                path = convert_cue_file(path)
            rest = match.group(4) or ''
            yield '{0} {1} "{2}"{3}'.format(cmd, drive, path, rest)
            continue
        match = change_drv.match(line)
        if match:
            drive = match.group(1).upper()
            yield drive
            continue
        yield line


def parse_dosbox_arguments(args):
    """Parse subset of DOSBox command line arguments."""
    parser = argparse.ArgumentParser()
    parser.add_argument('-conf', action='append')
    parser.add_argument('-c', action='append', nargs='?')
    parser.add_argument('-noautoexec', action='store_true')
    parser.add_argument('-noconsole', action='store_true')
    parser.add_argument('-fullscreen', action='store_true')
    parser.add_argument('-exit', action='store_true')
    parser.add_argument('file', nargs='?')
    args = parser.parse_args(args)
    cmds = list(filter(lambda x: x, args.c or []))
    args.c = cmds
    return args


def create_dosbox_configuration(dosbox_args, tweak_conf):
    """Interpret DOSBox configuration."""
    args = parse_dosbox_arguments(dosbox_args)
    if not (args.conf or args.c or args.file):
        log_err("these are not DOSBox commandline arguments.")
        return None
    conf = DosboxConfiguration(conf_files=(args.conf or []),
                               commands=args.c,
                               exe=args.file,
                               noautoexec=args.noautoexec,
                               exit_after_exe=args.exit,
                               tweak_conf=tweak_conf)
    return conf


def create_user_conf_file(name, conf, dosbox_args):
    """Create DOSBox configuration file for user.

    Different sections are chosen either by this module, copied from
    existing .conf files, generated based on '-c' DOSBox argument or
    generated from a file pointed to be run.
    """
    assert name
    with open(name, 'w', encoding=conf.encoding) as conf_file:
        conf_file.write(COMMENT_SECTION.format(dosbox_args))
        conf_file.write(SDL_SECTION_2)
        if conf.has_section('render'):
            conf_file.write(RENDER_SECTION_2)
            for key, val in conf['render'].items():
                if key == 'frameskip':
                    # This option is useless nowadays, let's hide it.
                    continue
                if key in ('scaler', 'aspect'):
                    # Publishers sometimes pick weird scalers by default.
                    # We don't want their choice, but let's signal to the
                    # user, that here's the place to override the value.
                    #
                    # Same goes for aspect - it's common for publishers
                    # to misconfigure it and we inject game-specific
                    # default to auto.conf already.
                    #
                    conf_file.write('# {0}={1}\n'.format(key, val))
                    continue
                conf_file.write('{0}={1}\n'.format(key, val))
            conf_file.write('\n')

        if conf.has_section('autoexec'):
            conf_file.write('[autoexec]\n')
            for line in to_linux_autoexec(conf['autoexec']):
                conf_file.write(line + '\n')


def write_sdl_section(file):
    """Write sdl section."""
    if settings.finalized:
        sdl_fullresolution = settings.get_dosbox_fullresolution()
        file.write(SDL_SECTION_1.format(resolution=sdl_fullresolution))


def write_render_section(conf, file):
    """Write render section."""
    render_aspect = 'true'
    if conf and conf.has_section('render'):
        render_aspect = conf['render'].get('force_aspect', render_aspect)
    file.write(
        RENDER_SECTION_1.format(scaler=settings.get_dosbox_scaler(),
                                aspect=render_aspect))


def write_mixer_section(conf, file):
    """Write mixer section."""
    if conf.has_section('mixer'):
        file.write('[mixer]\n')
        for key, val in conf['mixer'].items():
            file.write('{0}={1}\n'.format(key, val))
        file.write('\n')


def write_sblaster_section(conf, file):
    """Write sound blaster section."""
    base, irq, dma, hdma = 220, 7, 1, 5  # DOSBox defaults
    if conf and conf.has_section('sblaster'):
        irq = conf['sblaster'].get('force_irq', str(irq))
    log('Setting up DOSBox audio:')
    print_err(SBLASTER_INFO.format(base=base, irq=irq, dma=dma))
    file.write(SBLASTER_SECTION.format(base=base, irq=irq, dma=dma, hdma=hdma))


def write_midi_section(file):
    """Write midi section."""
    mport = midi.find_midi_port()
    if mport:
        log('Detected', mport.name, 'on', mport.addr)
        print_err(MIDI_INFO)
        file.write(MIDI_SECTION.format(port=mport.addr))
    else:
        print_err(MIDI_INFO_NA)


def write_dos_section(conf, file):
    """Write dos section."""
    if conf and conf.has_section('dos'):
        dos_xms = conf['dos'].get('xms', 'true')
        dos_ems = conf['dos'].get('ems', 'true')
        dos_umb = conf['dos'].get('umb', 'true')
        file.write(DOS_SECTION.format(xms=dos_xms, ems=dos_ems, umb=dos_umb))


def create_auto_conf_file(conf):
    """Create DOSBox configuration file based on environment.

    Different sections are either hard-coded or generated based on
    user environment (used midi port, current screen resolution, etc.).
    """
    name = 'boxtron_auto.conf'
    with open(name, 'w') as auto:
        auto.write('# Generated by Boxtron\n')
        auto.write('# This file is re-created on every run\n\n')
        write_sdl_section(auto)
        write_render_section(conf, auto)
        auto.write(CPU_SECTION)
        write_mixer_section(conf, auto)
        write_sblaster_section(conf, auto)
        write_midi_section(auto)
        write_dos_section(conf, auto)
    return name
