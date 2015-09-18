# Copyright (C) 2015  Chris Lalancette <clalancette@gmail.com>

# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation;
# version 2.1 of the License.

# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.

# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301  USA

import struct
import time
import bisect
import collections
import StringIO
import socket

# There are a number of specific ways that numerical data is stored in the
# ISO9660/Ecma-119 standard.  In the text these are reference by the section
# number they are stored in.  A brief synopsis:
#
# 7.1.1 - 8-bit number
# 7.2.3 - 16-bit number, stored first as little-endian then as big-endian (4 bytes total)
# 7.3.1 - 32-bit number, stored as little-endian
# 7.3.2 - 32-bit number ,stored as big-endian
# 7.3.3 - 32-bit number, stored first as little-endian then as big-endian (8 bytes total)

VOLUME_DESCRIPTOR_TYPE_BOOT_RECORD = 0
VOLUME_DESCRIPTOR_TYPE_PRIMARY = 1
VOLUME_DESCRIPTOR_TYPE_SUPPLEMENTARY = 2
VOLUME_DESCRIPTOR_TYPE_VOLUME_PARTITION = 3
VOLUME_DESCRIPTOR_TYPE_SET_TERMINATOR = 255

class PyIsoException(Exception):
    def __init__(self, msg):
        Exception.__init__(self, msg)

class ISODate(object):
    def parse(self, datestr):
        raise NotImplementedError("Parse not yet implemented")
    def record(self):
        raise NotImplementedError("Record not yet implemented")
    def new(self, tm=None):
        raise NotImplementedError("New not yet implemented")

class HeaderVolumeDescriptor(object):
    def parse(self, vd, data_fp):
        raise PyIsoException("Child class must implement parse")

    def new(self, sys_ident, vol_ident, set_size, seqnum, log_block_size,
            vol_set_ident, pub_ident, preparer_ident, app_ident,
            copyright_file, abstract_file, bibli_file, vol_expire_date,
            app_use, flags):
        raise PyIsoException("Child class must implement new")

    def path_table_size(self):
        if not self.initialized:
            raise PyIsoException("This Volume Descriptor is not yet initialized")

        return self.path_tbl_size

    def add_path_table_record(self, ptr):
        if not self.initialized:
            raise PyIsoException("This Volume Descriptor is not yet initialized")
        # We keep the list of children in sorted order, based on the __lt__
        # method of the PathTableRecord object.
        bisect.insort_left(self.path_table_records, ptr)

    def path_table_record_be_equal_to_le(self, le_index, be_record):
        if not self.initialized:
            raise PyIsoException("This Volume Descriptor is not yet initialized")

        le_record = self.path_table_records[le_index]
        if be_record.len_di != le_record.len_di or \
           be_record.xattr_length != le_record.xattr_length or \
           swab_32bit(be_record.extent_location) != le_record.extent_location or \
           swab_16bit(be_record.parent_directory_num) != le_record.parent_directory_num or \
           be_record.directory_identifier != le_record.directory_identifier:
            return False
        return True

    def set_ptr_dirrecord(self, dirrecord):
        if not self.initialized:
            raise PyIsoException("This Volume Descriptor is not yet initialized")
        if dirrecord.is_root:
            ptr_index = 0
        else:
            ptr_index = self.find_ptr_index_matching_ident(dirrecord.file_ident)
        self.path_table_records[ptr_index].set_dirrecord(dirrecord)

    def find_ptr_index_matching_ident(self, child_ident):
        if not self.initialized:
            raise PyIsoException("This Volume Descriptor is not yet initialized")

        # This is equivalent to bisect.bisect_left() (and in fact the code is
        # modified from there).  However, we already overrode the __lt__ method
        # in PathTableRecord(), and we wanted our own comparison between two
        # strings, so we open-code it here.  Also note that the first entry in
        # self.path_table_records is always the root, and since we can't remove
        # the root we don't have to look at it.
        lo = 1
        hi = len(self.path_table_records)
        while lo < hi:
            mid = (lo + hi) // 2
            if ptr_lt(self.path_table_records[mid].directory_identifier, child_ident):
                lo = mid + 1
            else:
                hi = mid
        saved_ptr_index = lo

        if saved_ptr_index == len(self.path_table_records):
            raise PyIsoException("Could not find path table record!")

        return saved_ptr_index

    def add_to_space_size(self, addition_bytes):
        if not self.initialized:
            raise PyIsoException("This Volume Descriptor is not yet initialized")
        # The "addition" parameter is expected to be in bytes, but the space
        # size we track is in extents.  Round up to the next extent.
        self.space_size += ceiling_div(addition_bytes, self.log_block_size)

    def root_directory_record(self):
        if not self.initialized:
            raise PyIsoException("This Volume Descriptor is not yet initialized")

        return self.root_dir_record

    def logical_block_size(self):
        if not self.initialized:
            raise PyIsoException("This Volume Descriptor is not yet initialized")

        return self.log_block_size

class VolumeDescriptorDate(ISODate):
    '''
    A class to represent a Volume Descriptor Date as described in Ecma-119
    section 8.4.26.1.  The Volume Descriptor Date consists of a year (from 1 to
    9999), month (from 1 to 12), day of month (from 1 to 31), hour (from 0
    to 23), minute (from 0 to 59), second (from 0 to 59), hundredths of second,
    and offset from GMT in 15-minute intervals (from -48 to +52) fields.  There
    are two main ways to use this class: either to instantiate and then parse a
    string to fill in the fields (the parse() method), or to create a new entry
    with a tm structure (the new() method).
    '''
    def __init__(self):
        self.initialized = False
        self.time_fmt = "%Y%m%d%H%M%S"
        self.empty_string = '0'*16 + '\x00'

    def parse(self, datestr):
        '''
        Parse a Volume Descriptor Date out of a string.  A string of all zeros
        is valid, which means that the date in this field was not specified.

        Parameters:
          datestr - string to be parsed

        Returns:
          Nothing.
        '''
        if self.initialized:
            raise PyIsoException("This Volume Descriptor Date object is already initialized")

        if len(datestr) != 17:
            raise PyIsoException("Invalid ISO9660 date string")

        if datestr == self.empty_string or datestr == '\x00'*17:
            # Ecma-119, 8.4.26.1 specifies that if the string was all the
            # digit zero, with the last byte 0, the time wasn't specified.
            # However, in practice I have found that some ISOs specify this
            # field as all the number 0, so we allow both.
            self.year = 0
            self.month = 0
            self.dayofmonth = 0
            self.hour = 0
            self.minute = 0
            self.second = 0
            self.hundredthsofsecond = 0
            self.gmtoffset = 0
            self.present = False
        else:
            timestruct = time.strptime(datestr[:-3], self.time_fmt)
            self.year = timestruct.tm_year
            self.month = timestruct.tm_mon
            self.dayofmonth = timestruct.tm_mday
            self.hour = timestruct.tm_hour
            self.minute = timestruct.tm_min
            self.second = timestruct.tm_sec
            self.hundredthsofsecond = int(datestr[14:15])
            self.gmtoffset = struct.unpack("=b", datestr[16])
            self.present = True

        self.initialized = True
        self.date_str = datestr

    def record(self):
        '''
        Return the date string for this object.

        Parameters:
          None.

        Returns:
          Date as a string.
        '''
        if not self.initialized:
            raise PyIsoException("This Volume Descriptor Date is not yet initialized")

        return self.date_str

    def new(self, tm=None):
        '''
        Create a new Volume Descriptor Date.  If tm is None, then this Volume
        Descriptor Date will be full of zeros (meaning not specified).  If tm
        is not None, it is expected to be a struct_time object, at which point
        this Volume Descriptor Date object will be filled in with data from that
        struct_time.

        Parameters:
          tm - struct_time object to base new VolumeDescriptorDate off of,
               or None for an empty VolumeDescriptorDate.

        Returns:
          Nothing.
        '''
        if self.initialized:
            raise PyIsoException("This Volume Descriptor Date object is already initialized")

        if tm is not None:
            local = time.localtime(tm)
            self.year = local.tm_year
            self.month = local.tm_mon
            self.day_of_month = local.tm_mday
            self.hour = local.tm_hour
            self.minute = local.tm_min
            self.second = local.tm_sec
            self.hundredthsofsecond = 0
            self.gmtoffset = gmtoffset_from_tm(tm, local)
            self.date_str = time.strftime(self.time_fmt, local) + "{:0<2}".format(self.hundredthsofsecond) + struct.pack("=b", self.gmtoffset)
            self.present = True
        else:
            self.year = 0
            self.month = 0
            self.dayofmonth = 0
            self.hour = 0
            self.minute = 0
            self.second = 0
            self.hundredthsofsecond = 0
            self.gmtoffset = 0
            self.date_str = self.empty_string
            self.present = False

        self.initialized = True

class FileOrTextIdentifier(object):
    '''
    A class to represent a file or text identifier as specified in Ecma-119
    section 8.4.20 (Primary Volume Descriptor Publisher Identifier),
    section 8.4.21 (Primary Volume Descriptor Data Preparer Identifier),
    and section 8.4.22 (Primary Volume Descriptor Application Identifier).  This
    identifier can either be a text string or the name of a file.  If it is a
    file, then the first byte will be 0x5f, the file should exist in the root
    directory record, and the file should be ISO level 1 interchange compliant
    (no more than 8 characters for the name and 3 characters for the extension).
    There are two main ways to use this class: either to instantiate and then
    parse a string to fill in the fields (the parse() method), or to create a
    new entry with a text string and whether this is a filename or not (the
    new() method).
    '''
    def __init__(self):
        self.initialized = False

    def parse(self, ident_str, is_primary):
        '''
        Parse a file or text identifier out of a string.

        Parameters:
          ident_str  - The string to parse the file or text identifier from.
          is_primary - Boolean describing whether this identifier is part of the
                       primary volume descriptor.  If it is, and it describes
                       a file (not a free-form string), it must be in ISO
                       interchange level 1 (MS-DOS style 8.3 format).

        Returns:
          Nothing.
        '''
        if self.initialized:
            raise PyIsoException("This File or Text identifier is already initialized")
        self.text = ident_str
        # According to Ecma-119, 8.4.20, 8.4.21, and 8.4.22, if the first
        # byte is a 0x5f, then the rest of the field specifies a filename.
        # Ecma-119 is vague, but presumably if it is not a filename, then it
        # is an arbitrary text string.
        self.isfile = False
        if ident_str[0] == "\x5f":
            # If the identifier is in the PVD, Ecma-119 says that it must
            # specify a file at the root directory and the identifier must
            # be 8.3 (so interchange level 1).  If the identifier is in an SVD,
            # Ecma-119 places no restrictions on the length of the filename
            # (though it implicitly has to be less than 31 so it can fit in
            # a directory record).

            # First find the end of the filename, which should be a space.
            space_index = -1
            for index,val in enumerate(ident_str[1:]):
                if ident_str[index] == ' ':
                    # Once we see a space, we know we are at the end of the
                    # filename.
                    space_index = index
                    break

            if is_primary:
                if space_index == -1:
                    # Never found the end of the filename, throw an exception.
                    raise PyIsoException("Invalid filename for file identifier")

                interchange_level = 1
            else:
                if space_index == -1:
                    space_index = None
                interchange_level = 3

            self.filename = ident_str[1:space_index]
            check_iso9660_filename(self.filename, interchange_level)

            self.isfile = True
            self.text = ident_str[1:]

        self.initialized = True

    def new(self, text, isfile):
        '''
        Create a new file or text identifier.  If isfile is True, then this is
        expected to be the name of a file at the root directory (as specified
        in Ecma-119), and to conform to ISO interchange level 1 (for the PVD),
        or ISO interchange level 3 (for an SVD).

        Parameters:
          text   - The text to store into the identifier.
          isfile - Whether this identifier is free-form text, or refers to a
                   file.

        Returns:
          Nothing.
        '''
        if self.initialized:
            raise PyIsoException("This File or Text identifier is already initialized")

        if len(text) > 128:
            raise PyIsoException("Length of text must be <= 128")

        if isfile:
            # Note that we do not check for whether this file identifier is in
            # 8.3 format (a requirement for primary volume descriptors).  This
            # is because we don't want to expose this to an outside user of the
            # API, so instead we have the _check_filename() method below that
            # we call to do the checking at a later time.
            self.text = "{:<127}".format(text)
            self.filename = text
        else:
            self.text = "{:<128}".format(text)

        self.isfile = isfile
        self.initialized = True

    def is_file(self):
        '''
        Return True if this is a file identifier, False otherwise.

        Parameters:
          None.

        Returns:
          True if this identifier is a file, False if it is a free-form string.
        '''
        if not self.initialized:
            raise PyIsoException("This File or Text identifier is not yet initialized")
        return self.isfile

    def is_text(self):
        '''
        Returns True if this is a text identifier, False otherwise.

        Parameters:
          None.

        Returns:
          True if this identifier is a free-form file, False if it is a file.
        '''
        if not self.initialized:
            raise PyIsoException("This File or Text identifier is not yet initialized")
        return not self.isfile

    def record(self):
        '''
        Returns the file or text identification string suitable for recording.

        Parameters:
          None.

        Returns:
          The text representing this identifier.
        '''
        if not self.initialized:
            raise PyIsoException("This File or Text identifier is not yet initialized")
        if self.isfile:
            return "\x5f" + self.text
        # implicitly a text identifier
        return self.text

    def _check_filename(self, is_primary):
        '''
        Checks whether the identifier stored in this object is a file, and if
        so, the
        '''
        if not self.initialized:
            raise PyIsoException("This File or Text identifier is not yet initialized")

        if self.isfile:
            interchange_level = 1
            if not is_primary:
                interchange_level = 3
            check_iso9660_filename(self.filename, interchange_level)

class DirectoryRecordDate(ISODate):
    '''
    A class to represent a Directory Record date as described in Ecma-119
    section 9.1.5.  The Directory Record date consists of the number of years
    since 1900, the month, the day of the month, the hour, the minute, the
    second, and the offset from GMT in 15 minute intervals.  There are two main
    ways to use this class: either to instantiate and then parse a string to
    fill in the fields (the parse() method), or to create a new entry with a
    tm structure (the new() method).
    '''
    def __init__(self):
        self.initialized = False
        self.fmt = "=BBBBBBb"

    def parse(self, datestr):
        '''
        Parse a Directory Record date out of a string.
        '''
        if self.initialized:
            raise PyIsoException("Directory Record Date already initialized")

        (self.years_since_1900, self.month, self.day_of_month, self.hour,
         self.minute, self.second,
         self.gmtoffset) = struct.unpack(self.fmt, datestr)

        self.initialized = True

    def new(self, tm=None):
        '''
        Create a new Directory Record date based on the current time.
        '''
        if self.initialized:
            raise PyIsoException("Directory Record Date already initialized")

        if tm is not None:
            raise PyIsoException("Directory Record Date does not support passing tm in")

        # This algorithm was ported from cdrkit, genisoimage.c:iso9660_date()
        tm = time.time()
        local = time.localtime(tm)
        self.years_since_1900 = local.tm_year - 1900
        self.month = local.tm_mon
        self.day_of_month = local.tm_mday
        self.hour = local.tm_hour
        self.minute = local.tm_min
        self.second = local.tm_sec
        self.gmtoffset = gmtoffset_from_tm(tm, local)
        self.initialized = True

    def record(self):
        '''
        Return a string representation of the Directory Record date.
        '''
        if not self.initialized:
            raise PyIsoException("Directory Record Date not initialized")

        return struct.pack(self.fmt, self.years_since_1900, self.month,
                           self.day_of_month, self.hour, self.minute,
                           self.second, self.gmtoffset)

# FIXME: we should allow the user to choose between version 1.09 and 1.12 of
# Rock Ridge.
class RockRidge(object):
    def __init__(self):
        self.rr_flags = None
        self.posix_name = ""
        self.posix_file_mode = None
        self.posix_file_links = None
        self.posix_user_id = None
        self.posix_group_id = None
        self.posix_serial_number = None
        self.extension_sequence = None
        self.ext_id = None
        self.ext_des = None
        self.ext_src = None
        self.dev_t_high = None
        self.dev_t_low = None
        self.initialized = False
        self.creation_time = None
        self.access_time = None
        self.modification_time = None
        self.attribute_change_time = None
        self.backup_time = None
        self.expiration_time = None
        self.effective_time = None
        self.time_flags = None
        self.is_first_dir_record_of_root = False

    def parse(self, record, is_first_dir_record_of_root, cdfp,
              logical_block_size):
        if self.initialized:
            raise PyIsoException("Rock Ridge extension already initialized")

        self.is_first_dir_record_of_root = is_first_dir_record_of_root

        # FIXME: I hate to pass the cdfp all the way down here, as it is a
        # layering violation, but I don't currently see a better way to do this.
        orig_fp_offset = cdfp.tell()
        offset = 0
        left = len(record)
        continue_block = None
        continue_block_offset = None
        continue_block_len = None
        while True:
            if left == 0 or left == 1:
                # FIXME: the breaking out on one isn't really right, but some
                # records seem to have an extra \x00 byte on the end.
                if continue_block is None:
                    break
                cdfp.seek(continue_block * logical_block_size + continue_block_offset)
                record = cdfp.read(continue_block_len)
                left = continue_block_len
                offset = 0
                continue_block = None
                continue_block_offset = None
                continue_block_len = None
                continue
            elif left < 4:
                raise PyIsoException("Not enough bytes left in the System Use field")

            if record[offset:offset+2] == 'SP':
                if left < 7 or not is_first_dir_record_of_root:
                    raise PyIsoException("Invalid SUSP SP record")

                print("SP record")
                # OK, this is the first Directory Record of the root
                # directory, which means we should check it for the SUSP/RR
                # extension, which is exactly 7 bytes and starts with 'SP'.
                (su_len, su_entry_version, check_byte1, check_byte2,
                 bytes_skipped) = struct.unpack("=BBBBB", record[offset+2:offset+7])

                if su_len != 7:
                    raise PyIsoException("Invalid length on rock ridge extension")
                if check_byte1 != 0xbe or check_byte2 != 0xef:
                    raise PyIsoException("Invalid check bytes on rock ridge extension")

            elif record[offset:offset+2] == 'RR':
                print("RR record")
                (su_len, su_entry_version, self.rr_flags) = struct.unpack("=BBB",
                                                                          record[offset+2:offset+5])

                if su_len != 5:
                    raise PyIsoException("Invalid length on rock ridge extension")
            elif record[offset:offset+2] == 'CE':
                print("CE record")
                (su_len, su_entry_version, bl_cont_area_le, bl_cont_area_be,
                 offset_cont_area_le, offset_cont_area_be,
                 len_cont_area_le, len_cont_area_be) = struct.unpack("=BBLLLLLL", record[offset+2:offset+28])
                if su_len != 28:
                    raise PyIsoException("Invalid length on rock ridge extension")

                continue_block = bl_cont_area_le
                continue_block_offset = offset_cont_area_le
                continue_block_len = len_cont_area_le
            elif record[offset:offset+2] == 'PX':
                print("PX record")
                (su_len,) = struct.unpack("=B", record[offset+2])
                # In Rock Ridge 1.09, the su_len here should be 36, while for
                # 1.12, the su_len here should be 44.
                if su_len == 36:
                    (su_entry_version, posix_file_mode_le, posix_file_mode_be,
                     posix_file_links_le, posix_file_links_be,
                     posix_file_user_id_le, posix_file_user_id_be,
                     posix_file_group_id_le,
                     posix_file_group_id_be) = struct.unpack("=BLLLLLLLL",
                                                             record[offset+3:offset+36])
                    posix_file_serial_number_le = 0
                elif su_len == 44:
                    (su_entry_version, posix_file_mode_le, posix_file_mode_be,
                     posix_file_links_le, posix_file_links_be,
                     posix_file_user_id_le, posix_file_user_id_be,
                     posix_file_group_id_le, posix_file_group_id_be,
                     posix_file_serial_number_le,
                     posix_file_serial_number_be) = struct.unpack("=BLLLLLLLLLL",
                                                                  record[offset+3:offset+44])
                else:
                    raise PyIsoException("Invalid length on rock ridge extension")

                self.posix_file_mode = posix_file_mode_le
                self.posix_file_links = posix_file_links_le
                self.posix_user_id = posix_file_user_id_le
                self.posix_group_id = posix_file_group_id_le
                self.posix_serial_number = posix_file_serial_number_le
            elif record[offset:offset+2] == 'PD':
                print("PD record")
                (su_len, su_entry_version) = struct.unpack("=BB", record[offset+2:offset+4])
            elif record[offset:offset+2] == 'ST':
                print("ST record")
                (su_len, su_entry_version) = struct.unpack("=BB", record[offset+2:offset+4])
                if su_len != 4:
                    raise PyIsoException("Invalid length on rock ridge extension")
            elif record[offset:offset+2] == 'ER':
                print("ER record")
                if not is_first_dir_record_of_root:
                    raise PyIsoException("Invalid SUSP ER record")
                (su_len, su_entry_version, len_id, len_des, len_src,
                 ext_ver) = struct.unpack("=BBBBBB", record[offset+2:offset+8])

                tmp = offset+8
                self.ext_id = record[tmp:tmp+len_id]
                tmp += len_id
                self.ext_des = ""
                if len_des > 0:
                    self.ext_des = record[tmp:tmp+len_des]
                    tmp += len_des
                self.ext_src = record[tmp:tmp+len_src]
                tmp += len_src
            elif record[offset:offset+2] == 'ES':
                print("ES record")
                (su_len, su_entry_version, self.extension_sequence) = struct.unpack("=BBB", record[offset+2:offset+5])
                if su_len != 5:
                    raise PyIsoException("Invalid length on rock ridge extension")
            elif record[offset:offset+2] == 'PN':
                print("PN record")
                (su_len, su_entry_version, dev_t_high_le, dev_t_high_be,
                 dev_t_low_le, dev_t_low_be) = struct.unpack("=BBLLLL", record[offset+2:offset+20])
                if su_len != 20:
                    raise PyIsoException("Invalid length on rock ridge extension")
                self.dev_t_high = dev_t_high_le
                self.dev_t_low = dev_t_low_le
            elif record[offset:offset+2] == 'SL':
                print("SL record")
                (su_len, su_entry_version, flags) = struct.unpack("=BBB", record[offset+2:offset+5])

                # FIXME: deal with continuation of SL records
            elif record[offset:offset+2] == 'NM':
                print("NM record")
                (su_len, su_entry_version, flags) = struct.unpack("=BBB", record[offset+2:offset+5])

                name_len = su_len - 5
                if (flags & 0x7) not in [0, 1, 2, 4]:
                    raise PyIsoException("Invalid Rock Ridge NM flags")

                if (flags & (1 << 1)) or (flags & (1 << 2)) or (flags & (1 << 5)) and name_len != 0:
                    raise PyIsoException("Invalid name in Rock Ridge NM entry")
                self.posix_name += record[offset+5:offset+name_len]

            elif record[offset:offset+2] == 'CL':
                print("CL record")
                (su_len, su_entry_version, child_log_block_num_le,
                 child_log_block_num_be) = struct.unpack("=BBLL", record[offset+2:offset+12])
                if su_len != 12:
                    raise PyIsoException("Invalid length on rock ridge extension")
                # FIXME: deal with child link
            elif record[offset:offset+2] == 'PL':
                print("PL record")
                (su_len, su_entry_version, parent_log_block_num_le,
                 parent_log_block_num_be) = struct.unpack("=BBLL", record[offset+2:offset+12])
                if su_len != 12:
                    raise PyIsoException("Invalid length on rock ridge extension")
                # FIXME: deal with parent link
            elif record[offset:offset+2] == 'RE':
                print("RE record")
                (su_len, su_entry_version) = struct.unpack("=BB", record[offset+2:offset+4])
                if su_len != 4:
                    raise PyIsoException("Invalid length on rock ridge extension")

                # FIXME: deal with relocated
            elif record[offset:offset+2] == 'TF':
                print("TF record")
                (su_len, su_entry_version, self.time_flags) = struct.unpack("=BBB", record[offset+2:offset+5])
                if su_len < 5:
                    raise PyIsoException("Not enough bytes in the TF record")

                tflen = 7
                datetype = DirectoryRecordDate
                if self.time_flags & (1 << 7):
                    tflen = 17
                    datetype = VolumeDescriptorDate
                tmp = offset+5
                if self.time_flags & (1 << 0):
                    self.creation_time = datetype()
                    self.creation_time.parse(record[tmp:tmp+tflen])
                    tmp += tflen
                if self.time_flags & (1 << 1):
                    self.access_time = datetype()
                    self.access_time.parse(record[tmp:tmp+tflen])
                    tmp += tflen
                if self.time_flags & (1 << 2):
                    self.modification_time = datetype()
                    self.modification_time.parse(record[tmp:tmp+tflen])
                    tmp += tflen
                if self.time_flags & (1 << 3):
                    self.attribute_change_time = datetype()
                    self.attribute_change_time.parse(record[tmp:tmp+tflen])
                    tmp += tflen
                if self.time_flags & (1 << 4):
                    self.backup_time = datetype()
                    self.backup_time.parse(record[tmp:tmp+tflen])
                    tmp += tflen
                if self.time_flags & (1 << 5):
                    self.expiration_time = datetype()
                    self.expiration_time.parse(record[tmp:tmp+tflen])
                    tmp += tflen
                if self.time_flags & (1 << 6):
                    self.effective_time = datetype()
                    self.effective_time.parse(record[tmp:tmp+tflen])
                    tmp += tflen
            elif record[offset:offset+2] == 'SF':
                print("SF record")
                (su_len, su_entry_version, virtual_file_size_high_le,
                 virtual_file_size_high_be, virtual_file_size_low_le,
                 virtual_file_size_low_be, table_depth) = struct.unpack("=BBLLLLB", record[offset+2:offset+21])
                if su_len != 21:
                    raise PyIsoException("Invalid length on rock ridge extension")
                # FIXME: deal with sparse files
            else:
                raise PyIsoException("Unknown SUSP record %s" % (record[offset:offset+2]))
            if su_entry_version != 1:
                raise PyIsoException("Invalid version on rock ridge extension")
            offset += su_len
            left -= su_len

        self.su_entry_version = 1
        cdfp.seek(orig_fp_offset)
        self.initialized = True

    def new(self, is_first_dir_record_of_root):
        if self.initialized:
            raise PyIsoException("Rock Ridge extension already initialized")

        self.su_entry_version = 1
        self.is_first_dir_record_of_root = is_first_dir_record_of_root

        # For RR record
        self.rr_flags = 0x81

        # For PX record
        self.posix_file_mode = 040555
        self.posix_file_links = 2
        self.posix_user_id = 0
        self.posix_group_id = 0
        self.posix_file_serial_number = 0

        # For TF record
        self.time_flags = 0x0e
        self.access_time = DirectoryRecordDate()
        self.access_time.new()
        self.modification_time = DirectoryRecordDate()
        self.modification_time.new()
        self.attribute_change_time = DirectoryRecordDate()
        self.attribute_change_time.new()

        self.initialized = True

    def _calc_tf_len(self):
        tf_each_size = 7
        if self.time_flags & (1 << 7):
            tf_each_size = 17
        tf_num = 0
        if self.time_flags & (1 << 0):
            tf_num += 1
        if self.time_flags & (1 << 1):
            tf_num += 1
        if self.time_flags & (1 << 2):
            tf_num += 1
        if self.time_flags & (1 << 3):
            tf_num += 1
        if self.time_flags & (1 << 4):
            tf_num += 1
        if self.time_flags & (1 << 5):
            tf_num += 1
        if self.time_flags & (1 << 6):
            tf_num += 1

        return 5 + tf_each_size*tf_num

    def record(self):
        if not self.initialized:
            raise PyIsoException("Rock Ridge extension not yet initialized")

        sp_record = ""
        if self.is_first_dir_record_of_root:
            sp_record = 'SP' + struct.pack("=BBBBB", 7, self.su_entry_version, 0xbe, 0xef, 0)

        rr_record = 'RR' + struct.pack("=BBB", 5, self.su_entry_version, self.rr_flags)

        px_record = 'PX' + struct.pack("=BBLLLLLLLL", 36, self.su_entry_version,
                                       self.posix_file_mode,
                                       swab_32bit(self.posix_file_mode),
                                       self.posix_file_links,
                                       swab_32bit(self.posix_file_links),
                                       self.posix_user_id,
                                       swab_32bit(self.posix_user_id),
                                       self.posix_group_id,
                                       swab_32bit(self.posix_group_id))

        tf_record = 'TF' + struct.pack("=BBB", self._calc_tf_len(), self.su_entry_version, self.time_flags)
        if self.creation_time is not None:
            tf_record += self.creation_time.record()
        if self.access_time is not None:
            tf_record += self.access_time.record()
        if self.modification_time is not None:
            tf_record += self.modification_time.record()
        if self.attribute_change_time is not None:
            tf_record += self.attribute_change_time.record()
        if self.backup_time is not None:
            tf_record += self.backup_time.record()
        if self.expiration_time is not None:
            tf_record += self.expiration_time.record()
        if self.effective_time is not None:
            tf_record += self.effective_time.record()

        ce_record = ''
        if self.is_first_dir_record_of_root:
            # FIXME: fill in the continuation block appropriately
            ce_record = 'CE' + struct.pack("=BBLLLLLL", 28, self.su_entry_version,
                                           0, swab_32bit(0), 0, swab_32bit(0),
                                           0, swab_32bit(0))

        return sp_record + rr_record + px_record + tf_record + ce_record

    def length(self):
        if not self.initialized:
            raise PyIsoException("Rock Ridge extension not yet initialized")

        # len(sp_record) = 7
        # len(rr_record) = 5
        # len(px_record) = 36
        # len(tf_record) = 5 + date_type*enabled_times
        # len(ce_record) = 28
        return 7 + 5 + 36 + self._calc_tf_len() + 28

class DirectoryRecord(object):
    FILE_FLAG_EXISTENCE_BIT = 0
    FILE_FLAG_DIRECTORY_BIT = 1
    FILE_FLAG_ASSOCIATED_FILE_BIT = 2
    FILE_FLAG_RECORD_BIT = 3
    FILE_FLAG_PROTECTION_BIT = 4
    FILE_FLAG_MULTI_EXTENT_BIT = 7

    DATA_ON_ORIGINAL_ISO = 1
    DATA_IN_EXTERNAL_FP = 2

    # 22 00 17 00 00 00 00 00 00 17 00 08 00 00 00 00 08 00 73 04 18 0c 0d 08 f0 02 00 00 01 00 00 01 01 00'
    # Len: 0x22 (34 bytes)
    # Xattr Len: 0x0
    # Extent Location: 0x17 (23)
    # Data Length: 0x800 (2048)
    # Years since 1900: 0x73 (115)
    # Month: 0x4 (4, April)
    # Day of Month: 0x18 (24)
    # Hour: 0xc (12)
    # Minute: 0xd (13)
    # Second: 0x08 (8)
    # GMT Offset: 0xf0 (-16)
    # File Flags: 0x2 (No existence, no directory, associated file, no record, no protection, no multi-extent)
    # File Unit Size: 0x0 (0)
    # Interleave Gap Size: 0x0 (0)
    # SeqNum: 0x1 (1)
    # Len Fi: 0x1 (1)
    # File Identifier: 0x0 (0, root directory)

    def __init__(self):
        self.initialized = False
        self.fmt = "=BBLLLL7sBBBHHB"

    def parse(self, record, data_fp, parent, logical_block_size):
        '''
        Parse a directory record out of a string.
        '''
        if self.initialized:
            raise PyIsoException("Directory Record already initialized")

        if len(record) > 255:
            # Since the length is supposed to be 8 bits, this should never
            # happen.
            raise PyIsoException("Directory record longer than 255 bytes!")

        (self.dr_len, self.xattr_len, extent_location_le, extent_location_be,
         data_length_le, data_length_be, dr_date, self.file_flags,
         self.file_unit_size, self.interleave_gap_size, seqnum_le, seqnum_be,
         self.len_fi) = struct.unpack(self.fmt, record[:33])

        if len(record) != self.dr_len:
            # The record we were passed doesn't have the same information in it
            # as the directory entry thinks it should
            raise PyIsoException("Length of directory entry doesn't match internal check")

        if extent_location_le != swab_32bit(extent_location_be):
            raise PyIsoException("Little-endian (%d) and big-endian (%d) extent location disagree" % (extent_location_le, swab_32bit(extent_location_be)))
        self.original_extent_loc = extent_location_le
        self.new_extent_loc = None

        if data_length_le != swab_32bit(data_length_be):
            raise PyIsoException("Little-endian and big-endian data length disagree")
        self.data_length = data_length_le

        if seqnum_le != swab_16bit(seqnum_be):
            raise PyIsoException("Little-endian and big-endian seqnum disagree")
        self.seqnum = seqnum_le

        self.date = DirectoryRecordDate()
        self.date.parse(dr_date)

        # OK, we've unpacked what we can from the beginning of the string.  Now
        # we have to use the len_fi to get the rest.

        self.curr_length = 0
        self.children = []
        self.is_root = False
        self.isdir = False
        self.parent = parent
        self.original_data_location = self.DATA_ON_ORIGINAL_ISO
        self.data_fp = data_fp

        self.rock_ridge = None

        if self.parent is None:
            self.is_root = True
            # A root directory entry should always be exactly 34 bytes.
            if self.dr_len != 34:
                raise PyIsoException("Root directory entry of invalid length!")
            # A root directory entry should always have 0 as the identifier.
            if record[33] != '\x00':
                raise PyIsoException("Invalid root directory entry identifier")
            self.file_ident = record[33]
            self.isdir = True
        else:
            record_offset = 33
            self.file_ident = record[record_offset:record_offset + self.len_fi]
            record_offset += self.len_fi
            if self.file_flags & (1 << self.FILE_FLAG_DIRECTORY_BIT):
                self.isdir = True

            if self.len_fi % 2 == 0:
                record_offset += 1
            # FIXME: passing data_fp is a hack; we happen to know it is always
            # the cdfp, but this is a gross layering violation.
            if len(record[record_offset:]) > 0:
                self.rock_ridge = RockRidge()
                is_first_dir_record_of_root = self.file_ident == '\x00' and parent.parent == None
                self.rock_ridge.parse(record[record_offset:],
                                      is_first_dir_record_of_root,
                                      data_fp, logical_block_size)

        if self.xattr_len != 0:
            if self.file_flags & (1 << self.FILE_FLAG_RECORD_BIT):
                raise PyIsoException("Record Bit not allowed with Extended Attributes")
            if self.file_flags & (1 << self.FILE_FLAG_PROTECTION_BIT):
                raise PyIsoException("Protection Bit not allowed with Extended Attributes")

        self.initialized = True

        return self.rock_ridge != None

    def _new(self, mangledname, parent, seqnum, isdir, pvd, length, rock_ridge):
        # Adding a new time should really be done when we are going to write
        # the ISO (in record()).  Ecma-119 9.1.5 says:
        #
        # "This field shall indicate the date and the time of the day at which
        # the information in the Extent described by the Directory Record was
        # recorded."
        #
        # We create it here just to have something in the field, but we'll
        # redo the whole thing when we are mastering.
        self.date = DirectoryRecordDate()
        self.date.new()

        if length > 2**32-1:
            raise PyIsoException("Maximum supported file length is 2^32-1")

        self.data_length = length
        # FIXME: if the length of the item is more than 2^32 - 1, and the
        # interchange level is 3, we should make duplicate directory record
        # entries so we can represent the whole file (see
        # http://wiki.osdev.org/ISO_9660, Size Limitations for a discussion of
        # this).

        self.file_ident = mangledname

        self.isdir = isdir

        self.seqnum = seqnum
        # For a new directory record entry, there is no original_extent_loc,
        # so we leave it at None.
        self.original_extent_loc = None
        self.len_fi = len(self.file_ident)
        self.dr_len = struct.calcsize(self.fmt) + self.len_fi
        self.dr_len += (self.dr_len % 2)

        # When adding a new directory, we always add a full extent.  This number
        # tracks how much of that block we are using so that we can figure out
        # if we need to allocate a new block.
        self.curr_length = 0

        # From Ecma-119, 9.1.6, the file flag bits are:
        #
        # Bit 0 - Existence - 0 for existence known, 1 for hidden
        # Bit 1 - Directory - 0 for file, 1 for directory
        # Bit 2 - Associated File - 0 for not associated, 1 for associated
        # Bit 3 - Record - 0 for structure not in xattr, 1 for structure in xattr
        # Bit 4 - Protection - 0 for no owner and group in xattr, 1 for owner and group in xattr
        # Bit 5 - Reserved
        # Bit 6 - Reserved
        # Bit 7 - Multi-extent - 0 for final directory record, 1 for not final directory record
        # FIXME: We probably want to allow the existence, associated file, xattr
        # record, and multi-extent bits to be set by the caller.
        self.file_flags = 0
        if self.isdir:
            self.file_flags |= (1 << self.FILE_FLAG_DIRECTORY_BIT)
        self.file_unit_size = 0 # FIXME: we don't support setting file unit size for now
        self.interleave_gap_size = 0 # FIXME: we don't support setting interleave gap size for now
        self.xattr_len = 0 # FIXME: we don't support xattrs for now
        self.children = []

        # Note: it is important that this object be initialized *before* we do
        # the extent shuffle below, otherwise we'll throw exceptions when trying
        # to set the extent for this new entry.
        self.initialized = True

        self.parent = parent
        if parent is None:
            # If no parent, then this is the root
            self.is_root = True
            self.new_extent_loc = 23
        else:
            self.is_root = False
            self.parent.add_child(self, pvd, False)

        self.rock_ridge = None
        if rock_ridge:
            self.rock_ridge = RockRidge()
            is_first_dir_record_of_root = self.file_ident == '\x00' and parent.parent == None
            self.rock_ridge.new(is_first_dir_record_of_root)
            self.dr_len += self.rock_ridge.length()

    def new_fp(self, fp, length, isoname, parent, seqnum, pvd, rock_ridge):
        if self.initialized:
            raise PyIsoException("Directory Record already initialized")

        self.original_data_location = self.DATA_IN_EXTERNAL_FP
        self.data_fp = fp
        self._new(isoname, parent, seqnum, False, pvd, length, rock_ridge)

    def new_root(self, seqnum, pvd):
        if self.initialized:
            raise PyIsoException("Directory Record already initialized")

        self._new('\x00', None, seqnum, True, pvd, 2048, False)

    def new_dot(self, root, seqnum, pvd, rock_ridge):
        if self.initialized:
            raise PyIsoException("Directory Record already initialized")

        self._new('\x00', root, seqnum, True, pvd, 2048, rock_ridge)

    def new_dotdot(self, root, seqnum, pvd, rock_ridge):
        if self.initialized:
            raise PyIsoException("Directory Record already initialized")

        self._new('\x01', root, seqnum, True, pvd, 2048, rock_ridge)

    def new_dir(self, name, parent, seqnum, pvd, rock_ridge):
        if self.initialized:
            raise PyIsoException("Directory Record already initialized")

        self._new(name, parent, seqnum, True, pvd, 2048, rock_ridge)

    def add_child(self, child, vd, parsing):
        '''
        A method to add a child to this object.  Note that this is called both
        during parsing and when adding a new object to the system, so it
        it shouldn't have any functionality that is not appropriate for both.
        '''
        if not self.initialized:
            raise PyIsoException("Directory Record not yet initialized")

        if not self.isdir:
            raise Exception("Trying to add a child to a record that is not a directory")

        # First ensure that this is not a duplicate.
        for c in self.children:
            if c.file_ident == child.file_ident:
                raise PyIsoException("Parent %s already has a child named %s" % (self.file_ident, child.file_ident))

        # We keep the list of children in sorted order, based on the __lt__
        # method of this object.
        bisect.insort_left(self.children, child)

        # Check if child.dr_len will go over a boundary; if so, increase our
        # data length.
        self.curr_length += child.directory_record_length()
        if self.curr_length > self.data_length:
            if parsing:
                raise PyIsoException("More records than fit into parent directory record; ISO is corrupt")
            # When we overflow our data length, we always add a full block.
            self.data_length += vd.logical_block_size()
            # This also increases the size of the complete volume, so update
            # that here.
            vd.add_to_space_size(vd.logical_block_size())

    def remove_child(self, child, index, pvd):
        if not self.initialized:
            raise PyIsoException("Directory Record not yet initialized")

        self.curr_length -= child.directory_record_length()
        if (self.data_length - self.curr_length) > pvd.logical_block_size():
            self.data_length -= pvd.logical_block_size()
            pvd.remove_from_space_size(pvd.logical_block_size())

        del self.children[index]

    def is_dir(self):
        if not self.initialized:
            raise PyIsoException("Directory Record not yet initialized")
        return self.isdir

    def is_file(self):
        if not self.initialized:
            raise PyIsoException("Directory Record not yet initialized")
        return not self.isdir

    def is_dot(self):
        if not self.initialized:
            raise PyIsoException("Directory Record not yet initialized")
        return self.file_ident == '\x00'

    def is_dotdot(self):
        if not self.initialized:
            raise PyIsoException("Directory Record not yet initialized")
        return self.file_ident == '\x01'

    def directory_record_length(self):
        if not self.initialized:
            raise PyIsoException("Directory Record not yet initialized")
        return self.dr_len

    def extent_location(self):
        if not self.initialized:
            raise PyIsoException("Directory Record not yet initialized")
        if self.new_extent_loc is None:
            return self.original_extent_loc
        return self.new_extent_loc

    def file_identifier(self):
        if not self.initialized:
            raise PyIsoException("Directory Record not yet initialized")
        if self.is_root:
            return '/'
        if self.file_ident == '\x00':
            return '.'
        if self.file_ident == '\x01':
            return '..'
        return self.file_ident

    def file_length(self):
        if not self.initialized:
            raise PyIsoException("Directory Record not yet initialized")
        return self.data_length

    def record(self):
        if not self.initialized:
            raise PyIsoException("Directory Record not yet initialized")

        # Ecma-119 9.1.5 says the date should reflect the time when the
        # record was written, so we make a new date now and use that to
        # write out the record.
        self.date = DirectoryRecordDate()
        self.date.new()

        pad = '\x00' * ((struct.calcsize(self.fmt) + self.len_fi) % 2)

        new_extent_loc = self.original_extent_loc
        if new_extent_loc is None:
            new_extent_loc = self.new_extent_loc

        ret = struct.pack(self.fmt, self.dr_len, self.xattr_len,
                          new_extent_loc, swab_32bit(new_extent_loc),
                          self.data_length, swab_32bit(self.data_length),
                          self.date.record(), self.file_flags,
                          self.file_unit_size, self.interleave_gap_size,
                          self.seqnum, swab_16bit(self.seqnum),
                          self.len_fi) + self.file_ident + pad

        if self.rock_ridge is not None:
            ret += self.rock_ridge.record()

        return ret

    def open_data(self, logical_block_size):
        if not self.initialized:
            raise PyIsoException("Directory Record not yet initialized")

        if self.isdir:
            raise PyIsoException("Cannot write out a directory")

        if self.original_data_location == self.DATA_ON_ORIGINAL_ISO:
            self.data_fp.seek(self.original_extent_loc * logical_block_size)
        else:
            self.data_fp.seek(0)

        return self.data_fp,self.data_length

    def update_location(self, extents):
        if not self.initialized:
            raise PyIsoException("Directory Record not yet initialized")

        if self.new_extent_loc is None:
            self.new_extent_loc = self.original_extent_loc
        self.new_extent_loc += extents

    def __lt__(self, other):
        # This method is used for the bisect.insort_left() when adding a child.
        # It needs to return whether self is less than other.  Here we use the
        # ISO9660 sorting order which is essentially:
        #
        # 1.  The \x00 is always the "dot" record, and is always first.
        # 2.  The \x01 is always the "dotdot" record, and is always second.
        # 3.  Other entries are sorted lexically; this does not exactly match
        #     the sorting method specified in Ecma-119, but does OK for now.
        #
        # FIXME: we need to implement Ecma-119 section 9.3 for the sorting
        # order; this essentially means padding out the shorter of the two with
        # 0x20 (spaces), then comparing byte-by-byte until they differ.
        if self.file_ident == '\x00':
            if other.file_ident == '\x00':
                return False
            return True
        if other.file_ident == '\x00':
            return False

        if self.file_ident == '\x01':
            if other.file_ident == '\x00':
                return False
            return True

        if other.file_ident == '\x01':
            # If self.file_ident was '\x00', it would have been caught above.
            return False
        return self.file_ident < other.file_ident

class PrimaryVolumeDescriptor(HeaderVolumeDescriptor):
    def __init__(self):
        self.initialized = False
        self.fmt = "=B5sBB32s32sQLL32sHHHHHHLLLLLL34s128s128s128s128s37s37s37s17s17s17s17sBB512s653s"

    def parse(self, vd, data_fp):
        if self.initialized:
            raise PyIsoException("This Primary Volume Descriptor is already initialized")

        # According to Ecma-119, we have to parse both the
        # little-endian and bit-endian versions of:
        #
        # Space Size
        # Set Size
        # Seq Num
        # Logical Block Size
        # Path Table Size
        # Path Table Location
        # Optional Path Table Location
        #
        # In doing this, we:
        # a) Check to make sure that the little-endian and big-endian
        # versions agree with each other.
        # b) Only store one type in the class, and generate the other one
        # as necessary.
        (self.descriptor_type, self.identifier, self.version, unused1,
         self.system_identifier, self.volume_identifier, unused2,
         space_size_le, space_size_be, unused3, set_size_le, set_size_be,
         seqnum_le, seqnum_be, logical_block_size_le, logical_block_size_be,
         path_table_size_le, path_table_size_be, self.path_table_location_le,
         self.optional_path_table_location_le, self.path_table_location_be,
         self.optional_path_table_location_be, root_dir_record,
         self.volume_set_identifier, pub_ident_str, prepare_ident_str,
         app_ident_str, self.copyright_file_identifier,
         self.abstract_file_identifier, self.bibliographic_file_identifier,
         vol_create_date_str, vol_mod_date_str, vol_expire_date_str,
         vol_effective_date_str, self.file_structure_version, unused4,
         self.application_use, unused5) = struct.unpack(self.fmt, vd)

        # According to Ecma-119, 8.4.1, the primary volume descriptor type
        # should be 1.
        if self.descriptor_type != VOLUME_DESCRIPTOR_TYPE_PRIMARY:
            raise PyIsoException("Invalid primary volume descriptor")
        # According to Ecma-119, 8.4.2, the identifier should be "CD001".
        if self.identifier != "CD001":
            raise PyIsoException("invalid CD isoIdentification")
        # According to Ecma-119, 8.4.3, the version should be 1.
        if self.version != 1:
            raise PyIsoException("Invalid primary volume descriptor version")
        # According to Ecma-119, 8.4.4, the first unused field should be 0.
        if unused1 != 0:
            raise PyIsoException("data in unused field not zero")
        # According to Ecma-119, 8.4.5, the second unused field (after the
        # system identifier and volume identifier) should be 0.
        if unused2 != 0:
            raise PyIsoException("data in 2nd unused field not zero")
        # According to Ecma-119, 8.4.9, the third unused field should be all 0.
        if unused3 != '\x00'*32:
            raise PyIsoException("data in 3rd unused field not zero")
        # According to Ecma-119, 8.4.30, the file structure version should be 1.
        if self.file_structure_version != 1:
            raise PyIsoException("File structure version expected to be 1")
        # According to Ecma-119, 8.4.31, the fourth unused field should be 0.
        if unused4 != 0:
            raise PyIsoException("data in 4th unused field not zero")
        # According to Ecma-119, the last 653 bytes of the PVD should be all 0.
        if unused5 != '\x00'*653:
            raise PyIsoException("data in 5th unused field not zero")

        # Check to make sure that the little-endian and big-endian versions
        # of the parsed data agree with each other.
        if space_size_le != swab_32bit(space_size_be):
            raise PyIsoException("Little-endian and big-endian space size disagree")
        self.space_size = space_size_le

        if set_size_le != swab_16bit(set_size_be):
            raise PyIsoException("Little-endian and big-endian set size disagree")
        self.set_size = set_size_le

        if seqnum_le != swab_16bit(seqnum_be):
            raise PyIsoException("Little-endian and big-endian seqnum disagree")
        self.seqnum = seqnum_le

        if logical_block_size_le != swab_16bit(logical_block_size_be):
            raise PyIsoException("Little-endian and big-endian logical block size disagree")
        self.log_block_size = logical_block_size_le

        if path_table_size_le != swab_32bit(path_table_size_be):
            raise PyIsoException("Little-endian and big-endian path table size disagree")
        self.path_tbl_size = path_table_size_le

        self.path_table_location_be = swab_32bit(self.path_table_location_be)

        self.publisher_identifier = FileOrTextIdentifier()
        self.publisher_identifier.parse(pub_ident_str, True)
        self.preparer_identifier = FileOrTextIdentifier()
        self.preparer_identifier.parse(prepare_ident_str, True)
        self.application_identifier = FileOrTextIdentifier()
        self.application_identifier.parse(app_ident_str, True)
        self.volume_creation_date = VolumeDescriptorDate()
        self.volume_creation_date.parse(vol_create_date_str)
        self.volume_modification_date = VolumeDescriptorDate()
        self.volume_modification_date.parse(vol_mod_date_str)
        self.volume_expiration_date = VolumeDescriptorDate()
        self.volume_expiration_date.parse(vol_expire_date_str)
        self.volume_effective_date = VolumeDescriptorDate()
        self.volume_effective_date.parse(vol_effective_date_str)
        self.root_dir_record = DirectoryRecord()
        self.root_dir_record.parse(root_dir_record, data_fp, None, self.log_block_size)

        self.path_table_records = []

        self.initialized = True

    def new(self, sys_ident, vol_ident, set_size, seqnum, log_block_size,
            vol_set_ident, pub_ident, preparer_ident, app_ident,
            copyright_file, abstract_file, bibli_file, vol_expire_date,
            app_use):
        if self.initialized:
            raise PyIsoException("This Primary Volume Descriptor is already initialized")

        self.descriptor_type = VOLUME_DESCRIPTOR_TYPE_PRIMARY
        self.identifier = "CD001"
        self.version = 1

        if len(sys_ident) > 32:
            raise PyIsoException("The system identifer has a maximum length of 32")
        self.system_identifier = "{:<32}".format(sys_ident)

        if len(vol_ident) > 32:
            raise PyIsoException("The volume identifier has a maximum length of 32")
        self.volume_identifier = "{:<32}".format(vol_ident)

        # The space_size is the number of extents (2048-byte blocks) in the
        # ISO.  We know we will at least have the system area (16 extents),
        # the PVD (1 extent), the Volume Terminator (2 extents), 2 extents
        # for the little endian path table record, 2 extents for the big endian
        # path table record, and 1 extent for the root directory record,
        # for a total of 24 extents to start with.
        self.space_size = 24
        self.set_size = set_size
        if seqnum > set_size:
            raise PyIsoException("Sequence number must be less than or equal to set size")
        self.seqnum = seqnum
        self.log_block_size = log_block_size
        # The path table size is in bytes, and is always at least 10 bytes
        # (for the root directory record).
        self.path_tbl_size = 10
        # By default the Little Endian Path Table record starts at extent 19
        # (right after the Volume Terminator).
        self.path_table_location_le = 19
        # By default the Big Endian Path Table record starts at extent 21
        # (two extents after the Little Endian Path Table Record).
        self.path_table_location_be = 21
        # FIXME: we don't support the optional path table location right now
        self.optional_path_table_location_le = 0
        self.optional_path_table_location_be = 0
        self.root_dir_record = DirectoryRecord()
        self.root_dir_record.new_root(seqnum, self)

        if len(vol_set_ident) > 128:
            raise PyIsoException("The maximum length for the volume set identifier is 128")
        self.volume_set_identifier = "{:<128}".format(vol_set_ident)

        self.publisher_identifier = pub_ident
        self.publisher_identifier._check_filename(True)

        self.preparer_identifier = preparer_ident
        self.preparer_identifier._check_filename(True)

        self.application_identifier = app_ident
        self.application_identifier._check_filename(True)

        self.copyright_file_identifier = "{:<37}".format(copyright_file)
        self.abstract_file_identifier = "{:<37}".format(abstract_file)
        self.bibliographic_file_identifier = "{:<37}".format(bibli_file)

        # We make a valid volume creation and volume modification date here,
        # but they will get overwritten during writeout.
        now = time.time()
        self.volume_creation_date = VolumeDescriptorDate()
        self.volume_creation_date.new(now)
        self.volume_modification_date = VolumeDescriptorDate()
        self.volume_modification_date.new(now)
        self.volume_expiration_date = VolumeDescriptorDate()
        self.volume_expiration_date.new(vol_expire_date)
        self.volume_effective_date = VolumeDescriptorDate()
        self.volume_effective_date.new(now)
        self.file_structure_version = 1

        if len(app_use) > 512:
            raise PyIsoException("The maximum length for the application use is 512")
        self.application_use = "{:<512}".format(app_use)

        self.path_table_records = []

        self.initialized = True

    def sequence_number(self):
        if not self.initialized:
            raise PyIsoException("This Primary Volume Descriptor is not yet initialized")

        return self.seqnum

    def set_sequence_number(self, seqnum):
        if not self.initialized:
            raise PyIsoException("This Primary Volume Descriptor is not yet initialized")

        if seqnum > self.set_size:
            raise PyIsoException("Sequence number larger than volume set size")

        self.seqnum = seqnum

    def set_set_size(self, set_size):
        if not self.initialized:
            raise PyIsoException("This Primary Volume Descriptor is not yet initialized")

        if set_size > (2**16 - 1):
            raise PyIsoException("Set size too large to fit into 16-bit field")

        self.set_size = set_size

    def remove_from_space_size(self, removal_bytes):
        if not self.initialized:
            raise PyIsoException("This Primary Volume Descriptor is not yet initialized")
        # The "removal" parameter is expected to be in bytes, but the space
        # size we track is in extents.  Round up to the next extent.
        self.space_size -= ceiling_div(removal_bytes, self.log_block_size)

    def add_entry(self, flen, ptr_size=0):
        if not self.initialized:
            raise PyIsoException("This Primary Volume Descriptor is not yet initialized")

        # First add to the path table size.
        self.path_tbl_size += ptr_size
        # path_table_location_be minus path_table_location_le gives us the
        # number of extents the path table is taking up.  We multiply that by
        # block size to get the number of bytes to determine if we will overflow
        # the extent.
        if self.path_tbl_size > (self.path_table_location_be - self.path_table_location_le) * self.log_block_size:
            # If we overflowed the little endian path table location, then we
            # need to move the big endian one down.  We always move down in
            # multiples of 4096, so 2 extents.
            self.path_table_location_be += 2
            # We also need to update the space size with this; since we are
            # adding two extents for the little and two for the big, add four
            # total extents.
            self.add_to_space_size(4 * self.log_block_size)
            # We also need to move the starting extent for the root directory
            # record down.
            self.root_dir_record.update_location(4)

        # Now add to the space size.
        self.add_to_space_size(flen)

        # Finally reshuffle the extents.
        self.reshuffle_extents()

        # After we've reshuffled the extents, we have to run through the list
        # of path table records and reset their extents appropriately.
        self._update_ptr_extent_locations()

    def remove_entry(self, flen, directory_ident=None):
        if not self.initialized:
            raise PyIsoException("This Primary Volume Descriptor is not yet initialized")

        # First remove from our space size.
        self.remove_from_space_size(flen)

        if directory_ident != None:
            ptr_index = self.find_ptr_index_matching_ident(directory_ident)

            # Next remove from the Path Table Record size.
            self.path_tbl_size -= PathTableRecord.record_length(self.path_table_records[ptr_index].len_di)
            current_extents = self.path_table_location_be - self.path_table_location_le
            new_extents = ceiling_div(self.path_tbl_size, 4096) * 2

            if new_extents > current_extents:
                # This should never happen.
                raise PyIsoException("This should never happen")
            elif new_extents < current_extents:
                self.path_table_location_be -= 2
                self.remove_from_space_size(4 * self.log_block_size)
                self.root_dir_record.update_location(-4)
                # implicit else, no work to do

            del self.path_table_records[ptr_index]

        self.reshuffle_extents()

        # After we've reshuffled the extents, we have to run through the list
        # of path table records and reset their extents appropriately.
        self._update_ptr_extent_locations()

    def record(self):
        if not self.initialized:
            raise PyIsoException("This Primary Volume Descriptor is not yet initialized")

        now = time.time()

        vol_create_date = VolumeDescriptorDate()
        vol_create_date.new(now)

        vol_mod_date = VolumeDescriptorDate()
        vol_mod_date.new(now)

        vol_effective_date = VolumeDescriptorDate()
        vol_effective_date.new(now)

        return struct.pack(self.fmt, self.descriptor_type, self.identifier,
                           self.version, 0, self.system_identifier,
                           self.volume_identifier, 0, self.space_size,
                           swab_32bit(self.space_size), '\x00'*32,
                           self.set_size, swab_16bit(self.set_size),
                           self.seqnum, swab_16bit(self.seqnum),
                           self.log_block_size, swab_16bit(self.log_block_size),
                           self.path_tbl_size, swab_32bit(self.path_tbl_size),
                           self.path_table_location_le,
                           self.optional_path_table_location_le,
                           swab_32bit(self.path_table_location_be),
                           self.optional_path_table_location_be,
                           self.root_dir_record.record(),
                           self.volume_set_identifier,
                           self.publisher_identifier.record(),
                           self.preparer_identifier.record(),
                           self.application_identifier.record(),
                           self.copyright_file_identifier,
                           self.abstract_file_identifier,
                           self.bibliographic_file_identifier,
                           vol_create_date.record(),
                           vol_mod_date.record(),
                           self.volume_expiration_date.record(),
                           self.volume_effective_date.record(),
                           self.file_structure_version, 0, self.application_use,
                           "\x00" * 653)

    def reshuffle_extents(self):
        # Here we re-walk the entire tree, re-assigning extents as necessary.
        dirs = collections.deque([(self.root_directory_record(), True)])
        current_extent = self.root_directory_record().extent_location()
        while dirs:
            dir_record,root_record = dirs.popleft()
            for child in dir_record.children:
                # Equivalent to child.is_dot(), but faster.
                if child.file_ident == '\x00':
                    # With a normal directory, the extent for itself was already
                    # assigned when the parent assigned extents to all of the
                    # children, so we don't increment the extent.  The root
                    # directory record is a special case, where there was no
                    # parent so we need to manually move the extent forward one.
                    if root_record:
                        child.new_extent_loc = current_extent
                        # Equivalent to ceiling_div(dir_record.data_length, self.log_block_size), but faster
                        current_extent += -(-dir_record.data_length // self.log_block_size)
                    else:
                        child.new_extent_loc = child.parent.extent_location()
                # Equivalent to child.is_dotdot(), but faster.
                elif child.file_ident == '\x01':
                    if root_record:
                        # Special case of the root directory record.  In this
                        # case, we assume that the dot record has already been
                        # added, and is the one before us.  We set the dotdot
                        # extent location to the same as the dot one.
                        child.new_extent_loc = child.parent.extent_location()
                    else:
                        child.new_extent_loc = child.parent.parent.extent_location()
                else:
                    child.new_extent_loc = current_extent
                    # We use child.isdir (instead of the is_dir() method)
                    # because it ends up being faster.
                    if child.isdir:
                        dirs.append((child, False))
                    # Equivalent to ceiling_div(child.data_length, self.log_block_size), but faster
                    current_extent += -(-child.data_length // self.log_block_size)

    def _update_ptr_extent_locations(self):
        for ptr in self.path_table_records:
            ptr.update_extent_location_from_dirrecord()

    def increment_ptr_extent(self):
        self.path_table_location_le += 1
        self.path_table_location_be += 1
        self.add_to_space_size(self.log_block_size)
        # We also need to move the starting extent for the root directory
        # record down.
        self.root_dir_record.update_location(1)

        self.reshuffle_extents()

        # After we've reshuffled the extents, we have to run through the list
        # of path table records and reset their extents appropriately.
        self._update_ptr_extent_locations()

    def decrement_ptr_extent(self):
        self.path_table_location_le -= 1
        self.path_table_location_be -= 1
        self.remove_from_space_size(self.log_block_size)
        # We also need to move the starting extent for the root directory
        # record down.
        self.root_dir_record.update_location(-1)

        self.reshuffle_extents()

        # After we've reshuffled the extents, we have to run through the list
        # of path table records and reset their extents appropriately.
        self._update_ptr_extent_locations()

class VolumeDescriptorSetTerminator(object):
    def __init__(self):
        self.initialized = False
        self.fmt = "=B5sB2041s"

    def parse(self, vd):
        if self.initialized:
            raise PyIsoException("Volume Descriptor Set Terminator already initialized")

        (self.descriptor_type, self.identifier, self.version,
         unused) = struct.unpack(self.fmt, vd)

        # According to Ecma-119, 8.3.1, the volume descriptor set terminator
        # type should be 255
        if self.descriptor_type != VOLUME_DESCRIPTOR_TYPE_SET_TERMINATOR:
            raise PyIsoException("Invalid descriptor type")
        # According to Ecma-119, 8.3.2, the identifier should be "CD001"
        if self.identifier != 'CD001':
            raise PyIsoException("Invalid identifier")
        # According to Ecma-119, 8.3.3, the version should be 1
        if self.version != 1:
            raise PyIsoException("Invalid version")
        # According to Ecma-119, 8.3.4, the rest of the terminator should be 0
        if unused != '\x00'*2041:
            raise PyIsoException("Invalid unused field")
        self.initialized = True

    def new(self):
        if self.initialized:
            raise PyIsoException("Volume Descriptor Set Terminator already initialized")

        self.descriptor_type = VOLUME_DESCRIPTOR_TYPE_SET_TERMINATOR
        self.identifier = "CD001"
        self.version = 1
        self.initialized = True

    def record(self):
        if not self.initialized:
            raise PyIsoException("Volume Descriptor Set Terminator not yet initialized")
        return struct.pack(self.fmt, self.descriptor_type,
                           self.identifier, self.version, "\x00" * 2041)

class EltoritoValidationEntry(object):
    def __init__(self):
        self.initialized = False
        # An Eltorito validation entry consists of:
        # Offset 0x0:       Header ID (0x1)
        # Offset 0x1:       Platform ID (0 for x86, 1 for PPC, 2 for Mac)
        # Offset 0x2-0x3:   Reserved, must be 0
        # Offset 0x4-0x1b:  ID String for manufacturer of CD
        # Offset 0x1c-0x1d: Checksum of all bytes.
        # Offset 0x1e:      Key byte 0x55
        # Offset 0x1f:      Key byte 0xaa
        self.fmt = "=BBH24sHBB"

    def _checksum(self, data):
        '''
        Method to compute the checksum on the ISO.  Note that this is *not*
        a 1's complement checksum; when an addition overflows, the carry
        bit is discarded, not added to the end.
        '''
        s = 0
        for i in range(0, len(data), 2):
            w = ord(data[i]) + (ord(data[i+1]) << 8)
            s = (s + w) & 0xffff
        return s

    def parse(self, valstr):
        if self.initialized:
            raise PyIsoException("Eltorito Validation Entry already initialized")

        (self.header_id, self.platform_id, reserved, self.id_string,
         self.checksum, self.keybyte1,
         self.keybyte2) = struct.unpack(self.fmt, valstr)

        if self.header_id != 1:
            raise PyIsoException("Eltorito Validation entry header ID not 1")

        if self.platform_id not in [0, 1, 2]:
            raise PyIsoException("Eltorito Validation entry platform ID not valid")

        if self.keybyte1 != 0x55:
            raise PyIsoException("Eltorito Validation entry first keybyte not 0x55")
        if self.keybyte2 != 0xaa:
            raise PyIsoException("Eltorito Validation entry second keybyte not 0xaa")

        # Now that we've done basic checking, calculate the checksum of the
        # validation entry and make sure it is right.
        if self._checksum(valstr) != 0:
            raise PyIsoException("Eltorito Validation entry checksum not correct")

        self.initialized = True

    def new(self):
        if self.initialized:
            raise PyIsoException("Eltorito Validation Entry already initialized")

        self.header_id = 1
        self.platform_id = 0 # FIXME: let the user set this
        self.id_string = "\x00"*24 # FIXME: let the user set this
        self.keybyte1 = 0x55
        self.keybyte2 = 0xaa
        self.checksum = 0
        self.checksum = swab_16bit(self._checksum(self._record()) - 1)
        self.initialized = True

    def _record(self):
        return struct.pack(self.fmt, self.header_id, self.platform_id, 0, self.id_string, self.checksum, self.keybyte1, self.keybyte2)

    def record(self):
        if not self.initialized:
            raise PyIsoException("Eltorito Validation Entry not yet initialized")

        return self._record()

class EltoritoInitialEntry(object):
    def __init__(self):
        self.initialized = False
        # An Eltorito initial entry consists of:
        # Offset 0x0:      Boot indicator (0x88 for bootable, 0x00 for
        #                  non-bootable)
        # Offset 0x1:      Boot media type.  One of 0x0 for no emulation,
        #                  0x1 for 1.2M diskette emulation, 0x2 for 1.44M
        #                  diskette emulation, 0x3 for 2.88M diskette
        #                  emulation, or 0x4 for Hard Disk emulation.
        # Offset 0x2-0x3:  Load Segment - if 0, use traditional 0x7C0.
        # Offset 0x4:      System Type - copy of Partition Table byte 5
        # Offset 0x5:      Unused, must be 0
        # Offset 0x6-0x7:  Sector Count - Number of virtual sectors to store
        #                  during initial boot.
        # Offset 0x8-0xb:  Load RBA - Start address of virtual disk.
        # Offset 0xc-0x1f: Unused, must be 0.
        self.fmt = "=BBHBBHL20s"

    def parse(self, valstr):
        if self.initialized:
            raise PyIsoException("Eltorito Initial Entry already initialized")

        (self.boot_indicator, self.boot_media_type, self.load_segment,
         self.system_type, unused1, self.sector_count, self.load_rba,
         unused2) = struct.unpack(self.fmt, valstr)

        if self.boot_indicator not in [0x88, 0x00]:
            raise PyIsoException("Invalid eltorito initial entry boot indicator")
        if self.boot_media_type > 4:
            raise PyIsoException("Invalid eltorito boot media type")

        # FIXME: check that the system type matches the partition table

        if unused1 != 0:
            raise PyIsoException("Eltorito unused field must be 0")

        if unused2 != '\x00'*20:
            raise PyIsoException("Eltorito unused end field must be all 0")

        self.initialized = True

    def new(self, initial_entry_extent):
        if self.initialized:
            raise PyIsoException("Eltorito Initial Entry already initialized")

        self.boot_indicator = 0x88 # FIXME: let the user set this
        self.boot_media_type = 0 # FIXME: let the user set this
        self.load_segment = 0x0 # FIXME: let the user set this
        self.system_type = 0
        self.sector_count = 4 # FIXME: this probably isn't right
        self.load_rba = initial_entry_extent

        self.initialized = True

    def record(self):
        if not self.initialized:
            raise PyIsoException("Eltorito Initial Entry not yet initialized")

        return struct.pack(self.fmt, self.boot_indicator, self.boot_media_type,
                           self.load_segment, self.system_type, 0,
                           self.sector_count, self.load_rba, '\x00'*20)

class EltoritoBootCatalog(object):
    def __init__(self):
        self.initialized = False

    def parse(self, valstr):
        if self.initialized:
            raise PyIsoException("Eltorito Boot Catalog already initialized")

        # A valid eltorito boot catalog must have a validation entry and an
        # initial entry.  The rest of the entries are optional.

        # The first entry in an Eltorito boot catalog is the Validation
        # Entry.  A Validation entry consists of 32 bytes (described in
        # detail in the parse_eltorito_valication_entry() method).
        self.validation_entry = EltoritoValidationEntry()
        self.validation_entry.parse(valstr[:32])

        # The next entry is the Initial/Default entry.  An Initial/Default
        # entry consists of 32 bytes (described in detail in the
        # parse_eltorito_initial_entry() method).
        self.initial_entry = EltoritoInitialEntry()
        self.initial_entry.parse(valstr[32:])

        self.initialized = True

    def record(self):
        if not self.initialized:
            raise PyIsoException("Eltorito Boot Catalog not yet initialized")

        return self.validation_entry.record() + self.initial_entry.record()

    def new(self, initial_entry_extent):
        if self.initialized:
            raise Exception("Eltorito Boot Catalog already initialized")

        # Create the Eltorito validation entry
        self.validation_entry = EltoritoValidationEntry()
        self.validation_entry.new()

        self.initial_entry = EltoritoInitialEntry()
        self.initial_entry.new(initial_entry_extent)

        self.initialized = True

class BootRecord(object):
    def __init__(self):
        self.initialized = False
        self.fmt = "=B5sB32s32s1977s"

    def parse(self, vd):
        if self.initialized:
            raise PyIsoException("Boot Record already initialized")

        (self.descriptor_type, self.identifier, self.version,
         self.boot_system_identifier, self.boot_identifier,
         self.boot_system_use) = struct.unpack(self.fmt, vd)

        # According to Ecma-119, 8.2.1, the boot record type should be 0
        if self.descriptor_type != VOLUME_DESCRIPTOR_TYPE_BOOT_RECORD:
            raise PyIsoException("Invalid descriptor type")
        # According to Ecma-119, 8.2.2, the identifier should be "CD001"
        if self.identifier != 'CD001':
            raise PyIsoException("Invalid identifier")
        # According to Ecma-119, 8.2.3, the version should be 1
        if self.version != 1:
            raise PyIsoException("Invalid version")

        self.initialized = True

    def record(self):
        if not self.initialized:
            raise PyIsoException("Boot Record not yet initialized")

        return struct.pack(self.fmt, self.descriptor_type, self.identifier,
                           self.version, self.boot_system_identifier,
                           self.boot_identifier, self.boot_system_use)

    def new(self, boot_system_id, boot_system_use):
        if self.initialized:
            raise Exception("Boot Record already initialized")

        self.descriptor_type = VOLUME_DESCRIPTOR_TYPE_BOOT_RECORD
        self.identifier = "CD001"
        self.version = 1
        self.boot_system_identifier = "{:\x00<32}".format(boot_system_id)
        self.boot_identifier = "\x00"*32 # FIXME: we may want to allow the user to set this
        self.boot_system_use = "{:\x00<197}".format(boot_system_use)
        self.initialized = True

class SupplementaryVolumeDescriptor(HeaderVolumeDescriptor):
    def __init__(self):
        self.initialized = False
        self.fmt = "=B5sBB32s32sQLL32sHHHHHHLLLLLL34s128s128s128s128s37s37s37s17s17s17s17sBB512s653s"
        self.path_table_records = []

    def parse(self, vd, data_fp):
        if self.initialized:
            raise PyIsoException("Supplementary Volume Descriptor already initialized")

        (self.descriptor_type, self.identifier, self.version, self.flags,
         self.system_identifier, self.volume_identifier, unused1,
         space_size_le, space_size_be, self.escape_sequences, set_size_le,
         set_size_be, seqnum_le, seqnum_be, logical_block_size_le,
         logical_block_size_be, path_table_size_le, path_table_size_be,
         self.path_table_location_le, self.optional_path_table_location_le,
         self.path_table_location_be, self.optional_path_table_location_be,
         root_dir_record, self.volume_set_identifier, pub_ident_str,
         prepare_ident_str, app_ident_str, self.copyright_file_identifier,
         self.abstract_file_identifier, self.bibliographic_file_identifier,
         vol_create_date_str, vol_mod_date_str, vol_expire_date_str,
         vol_effective_date_str, self.file_structure_version, unused2,
         self.application_use, unused3) = struct.unpack(self.fmt, vd)

        # According to Ecma-119, 8.5.1, the supplementary volume descriptor type
        # should be 2.
        if self.descriptor_type != VOLUME_DESCRIPTOR_TYPE_SUPPLEMENTARY:
            raise PyIsoException("Invalid supplementary volume descriptor")
        # According to Ecma-119, 8.4.2, the identifier should be "CD001".
        if self.identifier != "CD001":
            raise PyIsoException("invalid CD isoIdentification")
        # According to Ecma-119, 8.5.2, the version should be 1.
        if self.version != 1:
            raise PyIsoException("Invalid primary volume descriptor version")
        # According to Ecma-119, 8.4.5, the first unused field (after the
        # system identifier and volume identifier) should be 0.
        if unused1 != 0:
            raise PyIsoException("data in 2nd unused field not zero")
        if self.file_structure_version != 1:
            raise PyIsoException("File structure version expected to be 1")
        if unused2 != 0:
            raise PyIsoException("data in 4th unused field not zero")
        if unused3 != '\x00'*653:
            raise PyIsoException("data in 5th unused field not zero")

        # Check to make sure that the little-endian and big-endian versions
        # of the parsed data agree with each other
        if space_size_le != swab_32bit(space_size_be):
            raise PyIsoException("Little-endian and big-endian space size disagree")
        self.space_size = space_size_le

        if set_size_le != swab_16bit(set_size_be):
            raise PyIsoException("Little-endian and big-endian set size disagree")
        self.set_size = set_size_le

        if seqnum_le != swab_16bit(seqnum_be):
            raise PyIsoException("Little-endian and big-endian seqnum disagree")
        self.seqnum = seqnum_le

        if logical_block_size_le != swab_16bit(logical_block_size_be):
            raise PyIsoException("Little-endian and big-endian logical block size disagree")
        self.log_block_size = logical_block_size_le

        if path_table_size_le != swab_32bit(path_table_size_be):
            raise PyIsoException("Little-endian and big-endian path table size disagree")
        self.path_tbl_size = path_table_size_le

        self.path_table_location_be = swab_32bit(self.path_table_location_be)

        self.publisher_identifier = FileOrTextIdentifier()
        self.publisher_identifier.parse(pub_ident_str, False)
        self.preparer_identifier = FileOrTextIdentifier()
        self.preparer_identifier.parse(prepare_ident_str, False)
        self.application_identifier = FileOrTextIdentifier()
        self.application_identifier.parse(app_ident_str, False)
        self.volume_creation_date = VolumeDescriptorDate()
        self.volume_creation_date.parse(vol_create_date_str)
        self.volume_modification_date = VolumeDescriptorDate()
        self.volume_modification_date.parse(vol_mod_date_str)
        self.volume_expiration_date = VolumeDescriptorDate()
        self.volume_expiration_date.parse(vol_expire_date_str)
        self.volume_effective_date = VolumeDescriptorDate()
        self.volume_effective_date.parse(vol_effective_date_str)
        self.root_dir_record = DirectoryRecord()
        self.root_dir_record.parse(root_dir_record, data_fp, None, self.log_block_size)

        self.joliet = False
        if (self.flags & 0x1) == 0 and self.escape_sequences[:3] in ['%/@', '%/C', '%/E']:
            self.joliet = True
        self.initialized = True

    def new(self, flags, sys_ident, vol_ident, set_size, seqnum, log_block_size,
            vol_set_ident, pub_ident, preparer_ident, app_ident,
            copyright_file, abstract_file, bibli_file, vol_expire_date,
            app_use):
        if self.initialized:
            raise PyIsoException("This Supplementary Volume Descriptor is already initialized")

        self.descriptor_type = VOLUME_DESCRIPTOR_TYPE_SUPPLEMENTARY
        self.identifier = "CD001"
        self.version = 1
        self.flags = flags

        if len(sys_ident) > 32:
            raise PyIsoException("The system identifer has a maximum length of 32")
        self.system_identifier = "{:<32}".format(sys_ident)

        if len(vol_ident) > 32:
            raise PyIsoException("The volume identifier has a maximum length of 32")
        self.volume_identifier = "{:<32}".format(vol_ident)

        # The space_size is the number of extents (2048-byte blocks) in the
        # ISO.  We know we will at least have the system area (16 extents),
        # the PVD (1 extent), the Volume Terminator (2 extents), 2 extents
        # for the little endian path table record, 2 extents for the big endian
        # path table record, and 1 extent for the root directory record,
        # for a total of 24 extents to start with.
        self.space_size = 24
        self.set_size = set_size
        if seqnum > set_size:
            raise PyIsoException("Sequence number must be less than or equal to set size")
        self.seqnum = seqnum
        self.log_block_size = log_block_size
        # The path table size is in bytes, and is always at least 10 bytes
        # (for the root directory record).
        self.path_tbl_size = 10
        # By default the Little Endian Path Table record starts at extent 19
        # (right after the Volume Terminator).
        self.path_table_location_le = 19
        # By default the Big Endian Path Table record starts at extent 21
        # (two extents after the Little Endian Path Table Record).
        self.path_table_location_be = 21
        # FIXME: we don't support the optional path table location right now
        self.optional_path_table_location_le = 0
        self.optional_path_table_location_be = 0
        self.root_dir_record = DirectoryRecord()
        self.root_dir_record.new_root(seqnum, self)

        if len(vol_set_ident) > 128:
            raise PyIsoException("The maximum length for the volume set identifier is 128")
        self.volume_set_identifier = "{:<128}".format(vol_set_ident)

        self.publisher_identifier = pub_ident
        self.publisher_identifier._check_filename(True)

        self.preparer_identifier = preparer_ident
        self.preparer_identifier._check_filename(True)

        self.application_identifier = app_ident
        self.application_identifier._check_filename(True)

        self.copyright_file_identifier = "{:<37}".format(copyright_file)
        self.abstract_file_identifier = "{:<37}".format(abstract_file)
        self.bibliographic_file_identifier = "{:<37}".format(bibli_file)

        # We make a valid volume creation and volume modification date here,
        # but they will get overwritten during writeout.
        now = time.time()
        self.volume_creation_date = VolumeDescriptorDate()
        self.volume_creation_date.new(now)
        self.volume_modification_date = VolumeDescriptorDate()
        self.volume_modification_date.new(now)
        self.volume_expiration_date = VolumeDescriptorDate()
        self.volume_expiration_date.new(vol_expire_date)
        self.volume_effective_date = VolumeDescriptorDate()
        self.volume_effective_date.new(now)
        self.file_structure_version = 1

        if len(app_use) > 512:
            raise PyIsoException("The maximum length for the application use is 512")
        self.application_use = "{:<512}".format(app_use)

        self.path_table_records = []

        self.initialized = True

class VolumePartition(object):
    def __init__(self):
        self.initialized = False
        self.fmt = "=B5sBB32s32sLLLL1960s"

    def parse(self, vd):
        if self.initialized:
            raise PyIsoException("Volume Partition already initialized")

        (self.descriptor_type, self.identifier, self.version, unused,
         self.system_identifier, self.volume_partition_identifier,
         volume_partition_location_le, volume_partition_location_be,
         volume_partition_size_le, volume_partition_size_be,
         self.system_use) = struct.unpack(self.fmt, vd)

        # According to Ecma-119, 8.6.1, the volume partition type should be 3
        if self.descriptor_type != VOLUME_DESCRIPTOR_TYPE_VOLUME_PARTITION:
            raise PyIsoException("Invalid descriptor type")
        # According to Ecma-119, 8.6.2, the identifier should be "CD001"
        if self.identifier != 'CD001':
            raise PyIsoException("Invalid identifier")
        # According to Ecma-119, 8.6.3, the version should be 1
        if self.version != 1:
            raise PyIsoException("Invalid version")
        # According to Ecma-119, 8.6.4, the unused field should be 0
        if unused != 0:
            raise PyIsoException("Unused field should be zero")

        if volume_partition_location_le != swab_32bit(volume_partition_location_be):
            raise PyIsoException("Little-endian and big-endian volume partition location disagree")
        self.volume_partition_location = volume_partition_location_le

        if volume_partition_size_le != swab_32bit(volume_partition_size_be):
            raise PyIsoException("Little-endian and big-endian volume partition size disagree")
        self.volume_partition_size = volume_partition_size_le

        self.initialized = True

class ExtendedAttributeRecord(object):
    def __init__(self):
        self.initialized = False
        self.fmt = "=HHHHH17s17s17s17sBBHH32s64sBB64sHH"

    def parse(self, record):
        if self.initialized:
            raise PyIsoException("Extended Attribute Record already initialized")

        (owner_identification_le, owner_identification_be,
         group_identification_le, group_identification_be,
         self.permissions, file_create_date_str, file_mod_date_str,
         file_expire_date_str, file_effective_date_str,
         self.record_format, self.record_attributes, record_length_le,
         record_length_be, self.system_identifier, self.system_use,
         self.extended_attribute_record_version,
         self.length_of_escape_sequences, unused,
         len_au_le, len_au_be) = struct.unpack(self.fmt, record)

        if owner_identification_le != swab_16bit(owner_identification_be):
            raise PyIsoException("Little-endian and big-endian owner identification disagree")
        self.owner_identification = owner_identification_le

        if group_identification_le != swab_16bit(group_identification_be):
            raise PyIsoException("Little-endian and big-endian group identification disagree")
        self.group_identification = group_identification_le

        if record_length_le != swab_16bit(record_length_be):
            raise PyIsoException("Little-endian and big-endian record length disagree")
        self.record_length = record_length_le

        if len_au_le != swab_16bit(len_au_be):
            raise PyIsoException("Little-endian and big-endian record length disagree")
        self.len_au = len_au_le

        self.file_creation_date = VolumeDescriptorDate(file_create_date_str)
        self.file_modification_date = VolumeDescriptorDate(file_mod_date_str)
        self.file_expiration_date = VolumeDescriptorDate(file_expire_date_str)
        self.file_effective_date = VolumeDescriptorDate(file_effective_date_str)

        self.application_use = record[250:250 + self.len_au]
        self.escape_sequences = record[250 + self.len_au:250 + self.len_au + self.length_of_escape_sequences]

class PathTableRecord(object):
    FMT = "=BBLH"

    def __init__(self):
        self.initialized = False

    def parse(self, data):
        if self.initialized:
            raise PyIsoException("Path Table Record already initialized")

        (self.len_di, self.xattr_length, self.extent_location,
         self.parent_directory_num) = struct.unpack(self.FMT, data[:8])

        if self.len_di % 2 != 0:
            self.directory_identifier = data[8:-1]
        else:
            self.directory_identifier = data[8:]
        self.dirrecord = None
        if self.directory_identifier == '\x00':
            # For the root path table record, it's own directory num is 1
            self.directory_num = 1
        else:
            self.directory_num = self.parent_directory_num + 1
        self.initialized = True

    def _record(self, ext_loc, parent_dir_num):
        return struct.pack(self.FMT, self.len_di, self.xattr_length,
                           ext_loc, parent_dir_num) + self.directory_identifier + '\x00'*(self.len_di % 2)

    def record_little_endian(self):
        if not self.initialized:
            raise PyIsoException("Path Table Record not yet initialized")

        return self._record(self.extent_location, self.parent_directory_num)

    def record_big_endian(self):
        if not self.initialized:
            raise PyIsoException("Path Table Record not yet initialized")

        return self._record(swab_32bit(self.extent_location),
                            swab_16bit(self.parent_directory_num))

    @classmethod
    def record_length(self, len_di):
        # This method can be called even if the object isn't initialized
        return struct.calcsize(self.FMT) + len_di + (len_di % 2)

    def _new(self, name, dirrecord, parent_dir_num):
        self.len_di = len(name)
        self.xattr_length = 0 # FIXME: we don't support xattr for now
        self.extent_location = dirrecord.extent_location()
        self.parent_directory_num = parent_dir_num
        self.directory_identifier = name
        self.dirrecord = dirrecord
        if self.directory_identifier == '\x00':
            # For the root path table record, it's own directory num is 1
            self.directory_num = 1
        else:
            self.directory_num = self.parent_directory_num + 1
        self.initialized = True

    def new_root(self, dirrecord):
        if self.initialized:
            raise PyIsoException("Path Table Record already initialized")

        self._new("\x00", dirrecord, 1)

    def new_dir(self, name, dirrecord, parent_dir_num):
        if self.initialized:
            raise PyIsoException("Path Table Record already initialized")

        self._new(name, dirrecord, parent_dir_num)

    def set_dirrecord(self, dirrecord):
        if not self.initialized:
            raise PyIsoException("Path Table Record not yet initialized")

        self.dirrecord = dirrecord

    def update_extent_location_from_dirrecord(self):
        if not self.initialized:
            raise PyIsoException("Path Table Record not yet initialized")

        self.extent_location = self.dirrecord.extent_location()

    def __lt__(self, other):
        return ptr_lt(self.directory_identifier, other.directory_identifier)

def ptr_lt(str1, str2):
    # This method is used for the bisect.insort_left() when adding a child.
    # It needs to return whether str1 is less than str2.  Here we use the
    # ISO9660 sorting order which is essentially:
    #
    # 1.  The \x00 is always the "dot" record, and is always first.
    # 2.  The \x01 is always the "dotdot" record, and is always second.
    # 3.  Other entries are sorted lexically; this does not exactly match
    #     the sorting method specified in Ecma-119, but does OK for now.
    #
    # FIXME: we need to implement Ecma-119 section 9.3 for the sorting
    # order.
    if str1 == '\x00':
        # If both str1 and str2 are 0, then they are not strictly less.
        if str2 == '\x00':
            return False
        return True
    if str2 == '\x00':
        return False

    if str1 == '\x01':
        if str2 == '\x00':
            return False
        return True

    if str2 == '\x01':
        # If str1 was '\x00', it would have been caught above.
        return False
    return str1 < str2

def swab_32bit(input_int):
    return socket.htonl(input_int)

def swab_16bit(input_int):
    return socket.htons(input_int)

def pad(data_size, pad_size):
    pad = pad_size - (data_size % pad_size)
    if pad != pad_size:
        return "\x00" * pad
    return ""

def gmtoffset_from_tm(tm, local):
    gmtime = time.gmtime(tm)
    tmpyear = gmtime.tm_year - local.tm_year
    tmpyday = gmtime.tm_yday - local.tm_yday
    tmphour = gmtime.tm_hour - local.tm_hour
    tmpmin = gmtime.tm_min - local.tm_min

    if tmpyday < 0:
        tmpyday = -1
    else:
        if tmpyear > 0:
            tmpyday = 1
    return -(tmpmin + 60 * (tmphour + 24 * tmpyday)) / 15

def ceiling_div(numer, denom):
    # Doing division and then getting the ceiling is tricky; we do upside-down
    # floor division to make this happen.
    # See https://stackoverflow.com/questions/14822184/is-there-a-ceiling-equivalent-of-operator-in-python.
    return -(-numer // denom)

def check_d1_characters(name):
    for char in name:
        if not char in ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K',
                        'L', 'M', 'N', 'O', 'P', 'Q', 'R', 'S', 'T', 'U', 'V',
                        'W', 'X', 'Y', 'Z', '0', '1', '2', '3', '4', '5', '6',
                        '7', '8', '9', '_', '.']:
            raise PyIsoException("%s is not a valid ISO9660 filename (it contains invalid characters)" % (name))

def check_iso9660_filename(fullname, interchange_level):
    # Check to ensure the name is a valid filename for the ISO according to
    # Ecma-119 7.5.
    # First we split on the semicolon for the version number.
    namesplit = fullname.split(';')

    if len(namesplit) != 2:
        raise PyIsoException("%s is not a valid ISO9660 filename (it must have a version number at the end)" % (fullname))

    name_plus_extension = namesplit[0]
    version = namesplit[1]

    # The second entry should be the version number between 1 and 32767.
    if int(version) < 1 or int(version) > 32767:
        raise PyIsoException("%s has an invalid version number (must be between 1 and 32767" % (fullname))

    # The first entry should be x.y, so we split on the dot.
    dotsplit = name_plus_extension.split('.')
    if len(dotsplit) != 2:
        raise PyIsoException("%s is not a valid ISO9660 filename (it must have a dot)" % (fullname))

    name = dotsplit[0]
    extension = dotsplit[1]

    # Ecma-119 section 7.5.1 specifies that filenames must have at least one
    # character in either the name or the extension.
    if len(name) == 0 and len(extension) == 0:
        raise PyIsoException("%s is not a valid ISO9660 filename (either the name or extension must be non-empty" % (fullname))

    if interchange_level == 1:
        # According to Ecma-119, section 10.1, at level 1 the filename can
        # only be up to 8 d-characters or d1-characters, and the extension can
        # only be up to 3 d-characters or 3 d1-characters.
        if len(name) > 8 or len(extension) > 3:
            raise PyIsoException("%s is not a valid ISO9660 filename at interchange level 1" % (fullname))
    else:
        # For all other interchange levels, the maximum filename length is
        # specified in Ecma-119 7.5.2.
        if len(name) + len(extension) > 30:
            raise PyIsoException("%s is not a valid ISO9660 filename (the length of the name plus extension cannot exceed 30)" % (fullname))

    # Ecma-119 section 7.5.1 says that the file name and extension each contain
    # zero or more d-characters or d1-characters.  While the definition of
    # d-characters and d1-characters is not specified in Ecma-119,
    # http://wiki.osdev.org/ISO_9660 suggests that this consists of A-Z, 0-9, _
    # which seems to correlate with empirical evidence.  Thus we check for that
    # here.
    check_d1_characters(name)
    check_d1_characters(extension)

def check_iso9660_directory(fullname, interchange_level):
    # Check to ensure the directory name is valid for the ISO according to
    # Ecma-119 7.6.

    # Ecma-119 section 7.6.1 says that a directory identifier needs at least one
    # character
    if len(fullname) < 1:
        raise PyIsoException("%s is not a valid ISO9660 directory name (the name must have at least 1 character long)" % (fullname))

    if interchange_level == 1:
        # Ecma-119 section 10.1 says that directory identifiers lengths cannot
        # exceed 8 at interchange level 1.
        if len(fullname) > 8:
            raise PyIsoException("%s is not a valid ISO9660 directory name at interchange level 1" % (fullname))
    else:
        # Ecma-119 section 7.6.3 says that directory identifiers lengths cannot
        # exceed 31.
        if len(fullname) > 31:
            raise PyIsoException("%s is not a valid ISO9660 directory name (it is longer than 31 characters)" % (fullname))

    # Ecma-119 section 7.6.1 says that directory names consist of one or more
    # d-characters or d1-characters.  While the definition of d-characters and
    # d1-characters is not specified in Ecma-119,
    # http://wiki.osdev.org/ISO_9660 suggests that this consists of A-Z, 0-9, _
    # which seems to correlate with empirical evidence.  Thus we check for that
    # here.
    check_d1_characters(fullname)

def check_interchange_level(identifier, is_dir):
    interchange_level = 1
    cmpfunc = check_iso9660_filename
    if is_dir:
        cmpfunc = check_iso9660_directory

    try_level_3 = False
    try:
        # First we try to check for interchange level 1; if
        # that fails, we fall back to interchange level 3
        # and check that.
        cmpfunc(identifier, 1)
    except PyIsoException:
        try_level_3 = True

    if try_level_3:
        cmpfunc(identifier, 3)
        # If the above did not throw an exception, then this
        # is interchange level 3 and we should mark it.
        interchange_level = 3

    return interchange_level

def copy_data(data_length, blocksize, infp, outfp):
    left = data_length
    readsize = blocksize
    while left > 0:
        if left < readsize:
            readsize = left
        outfp.write(infp.read(readsize))
        left -= readsize

class PyIso(object):
    def _parse_volume_descriptors(self):
        # Ecma-119 says that the Volume Descriptor set is a sequence of volume
        # descriptors recorded in consecutively numbered Logical Sectors
        # starting with Logical Sector Number 16.  Since sectors are 2048 bytes
        # in length, we start at sector 16 * 2048
        pvds = []
        vdsts = []
        brs = []
        svds = []
        vpds = []
        # Ecma-119, 6.2.1 says that the Volume Space is divided into a System
        # Area and a Data Area, where the System Area is in logical sectors 0
        # to 15, and whose contents is not specified by the standard.
        self.cdfp.seek(16 * 2048)
        done = False
        while not done:
            # All volume descriptors are exactly 2048 bytes long
            curr_extent = self.cdfp.tell() / 2048
            vd = self.cdfp.read(2048)
            (desc_type,) = struct.unpack("=B", vd[0])
            if desc_type == VOLUME_DESCRIPTOR_TYPE_PRIMARY:
                pvd = PrimaryVolumeDescriptor()
                pvd.parse(vd, self.cdfp)
                pvds.append(pvd)
            elif desc_type == VOLUME_DESCRIPTOR_TYPE_SET_TERMINATOR:
                vdst = VolumeDescriptorSetTerminator()
                vdst.parse(vd)
                vdsts.append(vdst)
                # Once we see a set terminator, we stop parsing.  Oddly,
                # Ecma-119 says there may be multiple set terminators, but in
                # that case I don't know how to tell when we are done parsing
                # volume descriptors.  Leave this for now.
                done = True
            elif desc_type == VOLUME_DESCRIPTOR_TYPE_BOOT_RECORD:
                br = BootRecord()
                br.parse(vd)
                brs.append(br)
                self._check_and_parse_eltorito(br, curr_extent, pvds[0].logical_block_size())
            elif desc_type == VOLUME_DESCRIPTOR_TYPE_SUPPLEMENTARY:
                svd = SupplementaryVolumeDescriptor()
                svd.parse(vd, self.cdfp)
                svds.append(svd)
            elif desc_type == VOLUME_DESCRIPTOR_TYPE_VOLUME_PARTITION:
                vpd = VolumePartition()
                vpd.parse(vd)
                vpds.append(vpd)
        return pvds, svds, vpds, brs, vdsts

    def _seek_to_extent(self, extent):
        self.cdfp.seek(extent * self.pvd.logical_block_size())

    def _check_ident(self, fileortext, errmsg):
        if fileortext.is_file():
            try:
                self._find_record(self.pvd, "/" + fileortext.filename)
            except PyIsoException:
                raise PyIsoException("%s specifies a file of %s, but that file does not exist at the root level" % (errmsg, fileortext.filename))

    def _walk_directories(self, vd, do_check_interchange):
        vd.set_ptr_dirrecord(vd.root_directory_record())
        interchange_level = 1
        dirs = collections.deque([vd.root_directory_record()])
        block_size = vd.logical_block_size()
        while dirs:
            dir_record = dirs.popleft()
            self._seek_to_extent(dir_record.extent_location())
            length = dir_record.file_length()
            while length > 0:
                # read the length byte for the directory record
                (lenbyte,) = struct.unpack("=B", self.cdfp.read(1))
                length -= 1
                if lenbyte == 0:
                    # If we saw zero length, this may be a padding byte; seek
                    # to the start of the next extent.
                    if length > 0:
                        padsize = block_size - (self.cdfp.tell() % block_size)
                        padbytes = self.cdfp.read(padsize)
                        if padbytes != '\x00'*padsize:
                            # For now we are pedantic, and if the padding bytes
                            # are not all zero we throw an Exception.  Depending
                            # one what we see in the wild, we may have to loosen
                            # this check.
                            raise PyIsoException("Invalid padding on ISO")
                        length -= padsize
                        if length < 0:
                            # For now we are pedantic, and if the length goes
                            # negative because of the padding we throw an
                            # exception.  Depending on what we see in the wild,
                            # we may have to loosen this check.
                            raise PyIsoException("Invalid padding on ISO")
                    continue
                new_record = DirectoryRecord()
                self.rock_ridge |= new_record.parse(struct.pack("=B", lenbyte) + self.cdfp.read(lenbyte - 1),
                                                    self.cdfp, dir_record,
                                                    self.pvd.logical_block_size())
                length -= lenbyte - 1
                if new_record.is_dir():
                    if not new_record.is_dot() and not new_record.is_dotdot():
                        if do_check_interchange:
                            interchange_level = max(interchange_level, check_interchange_level(new_record.file_identifier(), True))
                        dirs.append(new_record)
                        vd.set_ptr_dirrecord(new_record)
                else:
                    if do_check_interchange:
                        interchange_level = max(interchange_level, check_interchange_level(new_record.file_identifier(), False))
                dir_record.add_child(new_record, vd, True)

        return interchange_level

    def _initialize(self):
        self.cdfp = None
        self.pvd = None
        self.svds = []
        self.vpds = []
        self.brs = []
        self.vdsts = []
        self.eltorito_boot_catalog = None
        self.initialized = False
        self.rock_ridge = False

    def _parse_path_table(self, vd, extent, callback):
        self._seek_to_extent(extent)
        left = vd.path_table_size()
        while left > 0:
            ptr = PathTableRecord()
            (len_di,) = struct.unpack("=B", self.cdfp.read(1))
            read_len = PathTableRecord.record_length(len_di)
            # PathTableRecord.record_length() returns the length of the entire
            # path table record, but we've already read the len_di so read one
            # less.
            ptr.parse(struct.pack("=B", len_di) + self.cdfp.read(read_len - 1))
            left -= read_len
            callback(vd, ptr)

    def _little_endian_path_table(self, vd, ptr):
        vd.add_path_table_record(ptr)

    def _big_endian_path_table(self, vd, ptr):
        bisect.insort_left(self.tmp_be_path_table_records, ptr)

    def _find_record(self, vd, path, encoding='ascii'):
        if path[0] != '/':
            raise PyIsoException("Must be a path starting with /")

        # If the path is just the slash, we just want the root directory, so
        # get the children there and quit.
        if path == '/':
            return vd.root_directory_record(),0

        # Split the path along the slashes
        splitpath = path.split('/')
        # Skip past the first one, since it is always empty.
        splitindex = 1

        currpath = splitpath[splitindex].encode(encoding)
        splitindex += 1
        children = vd.root_directory_record().children
        index = 0
        while index < len(children):
            child = children[index]
            index += 1

            if child.is_dot() or child.is_dotdot():
                continue

            if child.file_identifier() != currpath:
                continue

            # We found the child, and it is the last one we are looking for;
            # return it.
            if splitindex == len(splitpath):
                # We have to remove one from the index since we incremented it
                # above.
                return child,index-1
            else:
                if child.is_dir():
                    children = child.children
                    index = 0
                    currpath = splitpath[splitindex].encode(encoding)
                    splitindex += 1

        raise PyIsoException("Could not find path %s" % (path))

    def _name_and_parent_from_path(self, iso_path):
        if iso_path[0] != '/':
            raise PyIsoException("Must be a path starting with /")

        # First we need to find the parent of this directory, and add this
        # one as a child.
        splitpath = iso_path.split('/')
        # Pop off the front, as it is always blank.
        splitpath.pop(0)
        if len(splitpath) > 7:
            # Ecma-119 Section 6.8.2.1 says that the number of levels in the
            # hierarchy shall not exceed eight.  However, since the root
            # directory must always reside at level 1 by itself, this gives us
            # an effective maximum hierarchy depth of 7.
            raise PyIsoException("Directory levels too deep (maximum is 7)")
        # Now take the name off.
        name = splitpath.pop()
        if len(splitpath) == 0:
            # This is a new directory under the root, add it there
            parent = self.pvd.root_directory_record()
        else:
            parent,index = self._find_record(self.pvd, '/' + '/'.join(splitpath))

        return (name, parent)

    def _check_and_parse_eltorito(self, br, extent, logical_block_size):
        if br.boot_system_identifier != "{:\x00<32}".format("EL TORITO SPECIFICATION"):
            return

        if self.eltorito_boot_catalog is not None:
            raise PyIsoException("Only one El Torito boot record is allowed")

        # According to the El Torito specification, section 2.0, the El
        # Torito boot record must be at extent 17.
        if extent != 17:
            raise PyIsoException("El Torito Boot Record must be at extent 17")

        # Now that we have verified that the BootRecord is an El Torito one
        # and that it is sane, we go on to parse the El Torito Boot Catalog.
        # Note that the Boot Catalog is stored as a file in the ISO, though
        # we ignore that for the purposes of parsing.

        self.eltorito_boot_catalog = EltoritoBootCatalog()
        eltorito_boot_catalog_extent, = struct.unpack("=L", br.boot_system_use[:4])

        old = self.cdfp.tell()
        self.cdfp.seek(eltorito_boot_catalog_extent * logical_block_size)
        self.eltorito_boot_catalog.parse(self.cdfp.read(32 + 32))
        self.cdfp.seek(old)
        # FIXME: we should deal with the extended sections of Eltorito here.

########################### PUBLIC API #####################################
    def __init__(self):
        self._initialize()

    def new(self, interchange_level=1, sys_ident="", vol_ident="", set_size=1,
            seqnum=1, log_block_size=2048, vol_set_ident="", pub_ident=None,
            preparer_ident=None, app_ident=None, copyright_file="",
            abstract_file="", bibli_file="", vol_expire_date=None, app_use="",
            joliet=False, rock_ridge=False):
        if self.initialized:
            raise PyIsoException("This object already has an ISO; either close it or create a new object")

        if interchange_level < 1 or interchange_level > 3:
            raise PyIsoException("Invalid interchange level (must be between 1 and 3)")

        self.interchange_level = interchange_level

        # First create the new PVD.
        if pub_ident is None:
            pub_ident = FileOrTextIdentifier()
            pub_ident.new("", False)
        if preparer_ident is None:
            preparer_ident = FileOrTextIdentifier()
            preparer_ident.new("", False)
        if app_ident is None:
            app_ident = FileOrTextIdentifier()
            app_ident.new("PyIso (C) 2015 Chris Lalancette", False)

        self.pvd = PrimaryVolumeDescriptor()
        self.pvd.new(sys_ident, vol_ident, set_size, seqnum, log_block_size,
                     vol_set_ident, pub_ident, preparer_ident, app_ident,
                     copyright_file, abstract_file, bibli_file,
                     vol_expire_date, app_use)

        # Now that we have the PVD, make the root path table record.
        ptr = PathTableRecord()
        ptr.new_root(self.pvd.root_directory_record())
        self.pvd.add_path_table_record(ptr)

        self.joliet_vd = None
        if joliet:
            # If the user requested Joliet, make the SVD to represent it here.
            svd = SupplementaryVolumeDescriptor()
            svd.new(0, sys_ident, vol_ident, set_size, seqnum, log_block_size,
                    vol_set_ident, pub_ident, preparer_ident, app_ident,
                    copyright_file, abstract_file, bibli_file, vol_expire_date,
                    app_use)
            self.svds = [svd]
            self.joliet_vd = svd

        # Also make the volume descriptor set terminator.
        vdst = VolumeDescriptorSetTerminator()
        vdst.new()
        self.vdsts = [vdst]

        # Finally, make the directory entries for dot and dotdot.
        dot = DirectoryRecord()
        dot.new_dot(self.pvd.root_directory_record(),
                    self.pvd.sequence_number(), self.pvd, rock_ridge)

        dotdot = DirectoryRecord()
        dotdot.new_dotdot(self.pvd.root_directory_record(),
                          self.pvd.sequence_number(), self.pvd, rock_ridge)

        self.pvd.reshuffle_extents()

        self.rock_ridge = rock_ridge

        self.initialized = True

    def open(self, fp):
        if self.initialized:
            raise PyIsoException("This object already has an ISO; either close it or create a new object")

        self.cdfp = fp

        # Get the Primary Volume Descriptor (pvd), the set of Supplementary
        # Volume Descriptors (svds), the set of Volume Partition
        # Descriptors (vpds), the set of Boot Records (brs), and the set of
        # Volume Descriptor Set Terminators (vdsts)
        pvds, self.svds, self.vpds, self.brs, self.vdsts = self._parse_volume_descriptors()
        if len(pvds) != 1:
            raise PyIsoException("Valid ISO9660 filesystems have one and only one Primary Volume Descriptors")
        if len(self.vdsts) < 1:
            raise PyIsoException("Valid ISO9660 filesystems must have at least one Volume Descriptor Set Terminators")

        self.pvd = pvds[0]

        # Now that we have the PVD, parse the Path Tables according to Ecma-119
        # section 9.4.  What we really want is a single representation of the
        # path table records, so we only place the little endian path table
        # records into the PVD class.  However, we want to ensure that the
        # big endian versions agree with the little endian ones (to make sure
        # it is a valid ISO).  To do this we collect the big endian records
        # into a sorted list (to mimic what the list is stored as in the PVD),
        # and then compare them at the end.

        # Little Endian first
        self._parse_path_table(self.pvd, self.pvd.path_table_location_le,
                               self._little_endian_path_table)

        # Big Endian next.
        self.tmp_be_path_table_records = []
        self._parse_path_table(self.pvd, self.pvd.path_table_location_be,
                               self._big_endian_path_table)

        for index,ptr in enumerate(self.tmp_be_path_table_records):
            if not self.pvd.path_table_record_be_equal_to_le(index, ptr):
                raise PyIsoException("Little-endian and big-endian path table records do not agree")

        # OK, so now that we have the PVD, we start at its root directory
        # record and find all of the files
        self.interchange_level = self._walk_directories(self.pvd, True)

        # The PVD is finished.  Now look to see if we need to parse the SVD.
        self.joliet_vd = None
        for svd in self.svds:
            if svd.joliet:
                if self.joliet_vd is not None:
                    raise PyIsoException("Only a single Joliet SVD is supported")
                self.joliet_vd = svd

                self._parse_path_table(svd, svd.path_table_location_le,
                                       self._little_endian_path_table)

                self._parse_path_table(svd, svd.path_table_location_be,
                                       self._big_endian_path_table)

                self._walk_directories(svd, False)

        self.initialized = True

    def print_tree(self):
        if not self.initialized:
            raise PyIsoException("This object is not yet initialized; call either open() or new() to create an ISO")
        print("%s (extent %d)" % (self.pvd.root_directory_record().file_identifier(), self.pvd.root_directory_record().extent_location()))

        dirs = [(self.pvd.root_directory_record(), "/")]
        while dirs:
            curr,path = dirs.pop(0)
            for child in curr.children:
                if child.is_dot() or child.is_dotdot():
                    continue

            print("%s%s (extent %d)" % (path, child.file_identifier(), child.extent_location()))
            if child.is_dir():
                dirs.append((child, "%s%s/" % (path, child.file_identifier())))

    def get_and_write(self, iso_path, outfp, blocksize=8192):
        if not self.initialized:
            raise PyIsoException("This object is not yet initialized; call either open() or new() to create an ISO")

        try_iso9660 = True
        if self.joliet_vd is not None:
            try:
                found_record,index = self._find_record(self.joliet_vd, iso_path, 'utf-16_be')
                try_iso9660 = False
            except PyIsoException:
                pass

        if try_iso9660:
            found_record,index = self._find_record(self.pvd, iso_path)

        data_fp,data_length = found_record.open_data(self.pvd.logical_block_size())

        copy_data(data_length, blocksize, data_fp, outfp)

    def write(self, outfp):
        if not self.initialized:
            raise PyIsoException("This object is not yet initialized; call either open() or new() to create an ISO")

        # Before we do anything here, we need to make sure that the files
        # for the PVD and SVD(s) publisher, data preparer, and application
        # fields exist (if they were specified as files).
        self._check_ident(self.pvd.publisher_identifier,
                          "Primary Volume Descriptor Publisher Identifier")
        self._check_ident(self.pvd.preparer_identifier,
                          "Primary Volume Descriptor Data Preparer Identifier")
        self._check_ident(self.pvd.application_identifier,
                          "Primary Volume Descriptor Application Identifier")

        for svd in self.svds:
            self._check_ident(svd.publisher_identifier,
                              "Supplementary Volume Descriptor Publisher Identifier")
            self._check_ident(svd.preparer_identifier,
                              "Supplementary Volume Descriptor Data Preparer Identifier")
            self._check_ident(svd.application_identifier,
                              "Supplementary Volume Descriptor Application Identifier")

        outfp.seek(0)

        # Ecma-119, 6.2.1 says that the Volume Space is divided into a System
        # Area and a Data Area, where the System Area is in logical sectors 0
        # to 15, and whose contents is not specified by the standard.  Thus
        # we skip the first 16 sectors.
        outfp.seek(16 * self.pvd.logical_block_size())

        # First write out the PVD.
        outfp.write(self.pvd.record())

        # Next write out the boot records.
        extent = 17
        for br in self.brs:
            outfp.seek(extent * self.pvd.logical_block_size())
            outfp.write(br.record())
            extent += 1

        # Next we write out the Volume Descriptor Terminators.
        for vdst in self.vdsts:
            outfp.write(vdst.record())

        # Next we write out the version block.
        # FIXME: In genisoimage, write.c:vers_write(), this "version descriptor"
        # is written out with the exact command line used to create the ISO
        # (if in debug mode, otherwise it is all zero).  However, there is no
        # mention of this in any of the specifications I've read so far.  Where
        # does it come from?
        outfp.write("\x00" * 2048)

        # Next we write out the Path Table Records, both in Little Endian and
        # Big-Endian formats.  We do this within the same loop, seeking back
        # and forth as necessary.
        le_offset = 0
        be_offset = 0
        for record in self.pvd.path_table_records:
            outfp.seek(self.pvd.path_table_location_le * self.pvd.logical_block_size() + le_offset)
            ret = record.record_little_endian()
            outfp.write(ret)
            le_offset += len(ret)

            outfp.seek(self.pvd.path_table_location_be * self.pvd.logical_block_size() + be_offset)
            ret = record.record_big_endian()
            outfp.write(ret)
            be_offset += len(ret)

        # Once we are finished with the loop, we need to pad out the Big
        # Endian version.  The Little Endian one was already properly padded
        # by the mere fact that we wrote things for the Big Endian version
        # in the right place.
        outfp.write(pad(be_offset, 4096))

        # Now we need to write out the actual files.  Note that in many cases,
        # we haven't yet read the file out of the original, so we need to do
        # that here.
        dirs = collections.deque([self.pvd.root_directory_record()])
        while dirs:
            curr = dirs.popleft()
            curr_dirrecord_offset = 0
            for child in curr.children:
                # Now matter what type the child is, we need to first write out
                # the directory record entry.
                dir_extent = child.parent.extent_location()

                outfp.seek(dir_extent * self.pvd.logical_block_size() + curr_dirrecord_offset)
                # Now write out the child
                recstr = child.record()
                outfp.write(recstr)
                curr_dirrecord_offset += len(recstr)

                if child.is_dir():
                    # If the child is a directory, and is not dot or dotdot, we
                    # want to descend into it to look at the children.
                    if not child.is_dot() and not child.is_dotdot():
                        dirs.append(child)
                    outfp.write(pad(outfp.tell(), self.pvd.logical_block_size()))
                else:
                    # If the child is a file, then we need to write the data to
                    # the output file.
                    data_fp,data_length = child.open_data(self.pvd.logical_block_size())
                    outfp.seek(child.extent_location() * self.pvd.logical_block_size())
                    copy_data(data_length, 8192, data_fp, outfp)
                    outfp.write(pad(data_length, self.pvd.logical_block_size()))

    def add_fp(self, fp, length, iso_path):
        if not self.initialized:
            raise PyIsoException("This object is not yet initialized; call either open() or new() to create an ISO")

        (name, parent) = self._name_and_parent_from_path(iso_path)

        check_iso9660_filename(name, self.interchange_level)

        rec = DirectoryRecord()
        rec.new_fp(fp, length, name, parent, self.pvd.sequence_number(), self.pvd, self.rock_ridge)
        self.pvd.add_entry(length)

    def add_directory(self, iso_path):
        if not self.initialized:
            raise PyIsoException("This object is not yet initialized; call either open() or new() to create an ISO")

        (name, parent) = self._name_and_parent_from_path(iso_path)

        check_iso9660_directory(name, self.interchange_level)

        rec = DirectoryRecord()
        rec.new_dir(name, parent, self.pvd.sequence_number(), self.pvd, self.rock_ridge)

        dot = DirectoryRecord()
        dot.new_dot(rec, self.pvd.sequence_number(), self.pvd, self.rock_ridge)

        dotdot = DirectoryRecord()
        dotdot.new_dotdot(rec, self.pvd.sequence_number(), self.pvd, self.rock_ridge)

        self.pvd.add_entry(self.pvd.logical_block_size(),
                           PathTableRecord.record_length(len(name)))

        # We always need to add an entry to the path table record
        if parent.is_root:
            ptr_index = 0
        else:
            ptr_index = self.pvd.find_ptr_index_matching_ident(parent.file_ident)

        ptr = PathTableRecord()
        ptr.new_dir(name, rec, self.pvd.path_table_records[ptr_index].directory_num)

        self.pvd.add_path_table_record(ptr)

    def rm_file(self, iso_path):
        if not self.initialized:
            raise PyIsoException("This object is not yet initialized; call either open() or new() to create an ISO")

        if iso_path[0] != '/':
            raise PyIsoException("Must be a path starting with /")

        child,index = self._find_record(self.pvd, iso_path)

        if not child.is_file():
            raise PyIsoException("Cannot remove a directory with rm_file (try rm_directory instead(")

        child.parent.remove_child(child, index, self.pvd)

        self.pvd.remove_entry(child.file_length())

    def rm_directory(self, iso_path):
        if not self.initialized:
            raise PyIsoException("This object is not yet initialized; call either open() or new() to create an ISO")

        if iso_path == '/':
            raise PyIsoException("Cannot remove base directory")

        child,index = self._find_record(self.pvd, iso_path)

        if not child.is_dir():
            raise PyIsoException("Cannot remove a file with rm_directory (try rm_file instead)")

        for c in child.children:
            if c.is_dot() or c.is_dotdot():
                continue
            raise PyIsoException("Directory must be empty to use rm_directory")

        child.parent.remove_child(child, index, self.pvd)

        self.pvd.remove_entry(child.file_length(), child.file_ident)

    def set_sequence_number(self, seqnum):
        if not self.initialized:
            raise PyIsoException("This object is not yet initialized; call either open() or new() to create an ISO")

        self.pvd.set_sequence_number(seqnum)

        # FIXME: if this changes, we need to propagate it to all of the
        # Directory Record entries

    def set_set_size(self, set_size):
        if not self.initialized:
            raise PyIsoException("This object is not yet initialized; call either open() or new() to create an ISO")

        self.pvd.set_set_size(set_size)

    # FIXME: we might need an API call to manipulate permission bits on
    # individual files.

    def add_eltorito(self, bootfile_path, bootcatfile="BOOT.CAT;1"):
        if not self.initialized:
            raise PyIsoException("This object is not yet initialized; call either open() or new() to create an ISO")

        if self.eltorito_boot_catalog is not None:
            raise PyIsoException("This ISO already has an Eltorito Boot Record")

        # In order to add an El Torito boot, we need to do the following:
        # 1.  Find the boot file record (which must already exist).
        # 2.  Construct a BootCatalog.
        # 3.  Add the BootCatalog file to the filesystem.  When this step is
        #     over, we will know the extent that the file lives at.
        # 4.  Add the boot record to the ISO.

        # Step 1.
        child,index = self._find_record(self.pvd, bootfile_path)

        # Step 2.
        self.eltorito_boot_catalog = EltoritoBootCatalog()
        self.eltorito_boot_catalog.new(child.extent_location())

        # Step 3.
        fp = StringIO.StringIO()
        fp.write(self.eltorito_boot_catalog.record())
        fp.seek(0)
        (name, parent) = self._name_and_parent_from_path(bootcatfile)

        check_iso9660_filename(name, self.interchange_level)

        rec = DirectoryRecord()
        length = len(fp.getvalue())
        rec.new_fp(fp, length, name, parent, self.pvd.sequence_number(), self.pvd, self.rock_ridge)
        self.pvd.add_entry(length)

        # Step 4.
        br = BootRecord()
        br.new("EL TORITO SPECIFICATION", struct.pack("=L", rec.extent_location()))
        self.brs.append(br)

        self.pvd.increment_ptr_extent()

    def remove_eltorito(self):
        if not self.initialized:
            raise PyIsoException("This object is not yet initialized; call either open() or new() to create an ISO")

        if self.eltorito_boot_catalog is None:
            raise PyIsoException("This ISO doesn't have an Eltorito Boot Record")

        eltorito_index = None
        for index,br in enumerate(self.brs):
            if br.boot_system_identifier == "{:\x00<32}".format("EL TORITO SPECIFICATION"):
                eltorito_index = index
                break

        if eltorito_index is None:
            # There was a boot catalog, but no corresponding boot record.  This
            # should never happen.
            raise PyIsoException("El Torito boot catalog found with no corresponding boot record")

        extent, = struct.unpack("=L", br.boot_system_use[:4])

        del self.brs[eltorito_index]

        self.eltorito_boot_catalog = None

        self.pvd.decrement_ptr_extent()

        # Search through the filesystem, looking for the file that matches the
        # extent that the boot catalog lives at.
        dirs = [self.pvd.root_directory_record()]
        while dirs:
            curr = dirs.pop(0)
            for index,child in enumerate(curr.children):
                if child.is_dot() or child.is_dotdot():
                    continue

                if child.is_dir():
                    dirs.append(child)
                else:
                    if child.extent_location() == extent:
                        # We found the child
                        child.parent.remove_child(child, index, self.pvd)
                        self.pvd.remove_entry(child.file_length())
                        return

        raise PyIsoException("Could not find boot catalog file to remove!")

    def close(self):
        if not self.initialized:
            raise PyIsoException("This object is not yet initialized; call either open() or new() to create an ISO")

        # now that we are closed, re-initialize everything
        self._initialize()
