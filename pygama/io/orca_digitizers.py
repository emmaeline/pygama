import sys
import numpy as np

from .orcadaq import OrcaDecoder, get_ccc
from pygama.lh5 import Table

class ORCAStruck3302(OrcaDecoder):
    """
    ORCA decoder for Struck 3302 digitizer data
    """
    def __init__(self, *args, **kwargs):

        self.decoder_name = 'ORSIS3302DecoderForEnergy'
        self.orca_class_name = 'ORSIS3302Model'

        self.decoded_values_template = {
            'packet_id': {
               'dtype': 'uint32',
             },
            'ievt': {
              'dtype': 'uint32',
            },
            'energy': {
              'dtype': 'uint32',
              'units': 'adc',
            },
            'energy_first': {
              'dtype': 'uint32',
            },
            'timestamp': {
              'dtype': 'uint64',
              'units': 'clock_ticks',
            },
            'crate': {
              'dtype': 'uint8',
            },
            'card': {
              'dtype': 'uint8',
            },
            'channel': {
              'dtype': 'uint8',
            },
            'waveform': {
              'dtype': 'uint16',
              'datatype': 'waveform',
              'length': 65532, # max value. override this before initalizing buffers to save RAM
              'sample_period': 10, # override if a different clock rate is used
              'sample_period_units': 'ns',
              'units': 'adc',
            },
        }

        super().__init__(*args, **kwargs) # also initializes the garbage df

        self.decoded_values = {}
        self.ievt = 0
        self.skipped_channels = {}
        # self.enabled_cccs = []


    def get_decoded_values(self, channel=None):
        if channel is None:
            dec_vals_list = self.decoded_values.items()
            if len(dec_vals_list) == 0:
                print("ORSIS3302Model: Error: decoded_values not built yet!")
                return None
            return list(dec_vals_list)[0][1] # Get first thing we find
        if channel in self.decoded_values: return self.decoded_values[channel]
        print("ORSIS3302Model: Error: No decoded values for channel", channel)
        return None


    def set_object_info(self, object_info):
        self.object_info = object_info

        # parse object_info for important info
        for card_dict in self.object_info:
            crate = card_dict['Crate']
            card = card_dict['Card']

            int_enabled_mask = card_dict['internalTriggerEnabledMask']
            ext_enabled_mask = card_dict['externalTriggerEnabledMask']
            enabled_mask = int_enabled_mask | ext_enabled_mask
            for channel in range(10):
                # only care about enabled channels
                if not ((enabled_mask >> channel) & 0x1): continue

                ccc = get_ccc(crate, card, channel)
                # save list of enabled channels
                #self.enabled_cccs.append(ccc)

                self.decoded_values[ccc] = copy.deepcopy(self.decoded_values_template)
                sd = self.decoded_values[ccc] # alias

                # get trace length(s). Should all be the same until
                # multi-buffer mode is implemented AND each channel has its
                # own buffer
                trace_length = card_dict['sampleLengths'][int(channel/2)]
                if trace_length <= 0 or trace_length > 2**16:
                    print('SIS3316ORCADecoder Error: invalid trace_length', trace_length)
                    sys.exit()
                self.decoded_values[ccc]['waveform']['length'] = trace_length


    def max_n_rows_per_packet(self):
        return 1


    def decode_packet(self, packet, lh5_tables, packet_id, header_dict, verbose=False):
        """
        see README for the 32-bit data word diagram
        """
        # interpret the raw event data into numpy arrays of 16 and 32 bit ints
        # does not copy data. p32 and p16 are read-only
        p32 = np.frombuffer(packet, dtype=np.uint32)
        p16 = np.frombuffer(packet, dtype=np.uint16)

        # read the crate/card/channel first
        crate = (p32[0] >> 21) & 0xF
        card = (p32[0] >> 16) & 0x1F
        channel = (p32[0] >> 8) & 0xFF
        ccc = get_ccc(crate, card, channel)

        # aliases for brevity
        tb = lh5_tables
        # if the first key is an int, then there are different tables for
        # each channel.
        if isinstance(list(tb.keys())[0], int):
            if ccc not in lh5_tables:
                if ccc not in self.skipped_channels:
                    self.skipped_channels[ccc] = 0
                self.skipped_channels[ccc] += 1
                return
            tb = lh5_tables[ccc]
        ii = tb.loc

        # store packet id
        tb['packet_id'].nda[ii] = packet_id

        # read the rest of the record
        n_lost_msb = (p32[0] >> 25) & 0x7F
        n_lost_lsb = (p32[0] >> 2) & 0x7F
        n_lost_records = (n_lost_msb << 7) + n_lost_lsb
        tb['crate'].nda[ii] = (p32[0] >> 21) & 0xF
        tb['card'].nda[ii] = (p32[0] >> 16) & 0x1F
        tb['channel'].nda[ii] = (p32[0] >> 8) & 0xFF
        buffer_wrap = p32[0] & 0x1
        wf_length32 = p32[1]
        ene_wf_length32 = p32[2]
        evt_header_id = p32[3] & 0xFF
        tb['timestamp'].nda[ii] = p32[4] + ((p32[3] & 0xFFFF0000) << 16)
        last_word = p32[-1]

        # get the footer
        tb['energy'].nda[ii] = p32[-4]
        tb['energy_first'].nda[ii] = p32[-3]
        extra_flags = p32[-2]

        # compute expected and actual array dimensions
        wf_length16 = 2 * wf_length32
        orca_helper_length16 = 2
        sis_header_length16 = 12 if buffer_wrap else 8
        header_length16 = orca_helper_length16 + sis_header_length16
        ene_wf_length16 = 2 * ene_wf_length32
        footer_length16 = 8
        expected_wf_length = len(p16) - orca_helper_length16 - sis_header_length16 - \
            footer_length16 - ene_wf_length16

        # error check: waveform size must match expectations
        if wf_length16 != expected_wf_length or last_word != 0xdeadbeef:
            print(len(p16), orca_helper_length16, sis_header_length16,
                  footer_length16)
            print("ERROR: Waveform size %d doesn't match expected size %d." %
                  (wf_length16, expected_wf_length))
            print("       The Last Word (should be 0xdeadbeef):",
                  hex(last_word))
            exit()

        # indexes of stuff (all referring to the 16 bit array)
        i_wf_start = header_length16
        i_wf_stop = i_wf_start + wf_length16
        i_ene_start = i_wf_stop + 1
        i_ene_stop = i_ene_start + ene_wf_length16
        if buffer_wrap:
            # start somewhere in the middle of the record
            i_start_1 = p32[6] + header_length16 + 1
            i_stop_1 = i_wf_stop  # end of the wf record
            i_start_2 = i_wf_start  # beginning of the wf record
            i_stop_2 = i_start_1

        # handle the waveform(s)
        #energy_wf = np.zeros(ene_wf_length16)  # not used rn
        tbwf = tb['waveform']['values'].nda[ii]
        if wf_length32 > 0:
            if not buffer_wrap:
                if i_wf_stop - i_wf_start != expected_wf_length:
                    print("ERROR: event %d, we expected %d WF samples and only got %d" %
                          (ievt, expected_wf_length, i_wf_stope - i_wf_start))
                tbwf[:expected_wf_length] = p16[i_wf_start:i_wf_stop]
            else:
                len1 = i_stop_1-i_start_1
                len2 = i_stop_2-i_start_2
                if len1+len2 != expected_wf_length:
                    print("ERROR: event %d, we expected %d WF samples and only got %d" %
                          (ievt, expected_wf_length, len1+len2))
                    exit()
                tbwf[:len1] = p16[i_start_1:i_stop_1]
                tbwf[len1:len1+len2] = p16[i_start_2:i_stop_2]


        # set the event number (searchable HDF5 column)
        tb['ievt'].nda[ii] = self.ievt
        self.ievt += 1
        tb.push_row()


class ORCAGretina4M(OrcaDecoder):
    """
    Decoder for Majorana Gretina4M digitizer data

    NOTE: Tom Caldwell made some nice new summary slides on a 2019 LEGEND call
    https://indico.legend-exp.org/event/117/contributions/683/attachments/467/717/mjd_data_format.pdf
    """
    def __init__(self, *args, **kwargs):

        self.decoder_name = 'ORGretina4MWaveformDecoder'
        self.orca_class_name = 'ORGretina4MModel'

        self.decoded_values_template = {
            'packet_id': {
               'dtype': 'uint32',
             },
            'ievt': {
              'dtype': 'uint32',
            },
            'energy': {
              'dtype': 'uint32',
              'units': 'adc',
            },
            'timestamp': {
              'dtype': 'uint32',
              'units': 'clock_ticks',
            },
            'crate': {
              'dtype': 'uint8',
            },
            'card': {
              'dtype': 'uint8',
            },
            'channel': {
              'dtype': 'uint8',
            },
            "board_id": {
              'dtype': 'uint32',
            },
            'waveform': {
              'dtype': 'int16',
              'datatype': 'waveform',
              'length': 2016, # shorter if multispampling is used
              'sample_period': 10,
              'sample_period_units': 'ns',
              'units': 'adc',
            },
        }

        super().__init__(*args, **kwargs)
        self.decoded_values = {}
        self.ievt = 0
        self.skipped_channels = {}
        self.use_MS = False
        self.wf_skip = 16 # the first ~dozen samples are sometimes junk
        # channel pars for multisampling mode
        self.ft_len = {}
        self.ps = {}
        self.div = {}
        self.rises = np.zeros(2016)
        self.remainders = np.zeros(2016)


    def get_decoded_values(self, channel=None):
        if channel is None:
            dec_vals_list = self.decoded_values.items()
            if len(dec_vals_list) == 0:
                print("ORGretina4MModel: Error: decoded_values not built yet!")
                return None
            return list(dec_vals_list)[0][1] # Get first thing we find
        if channel in self.decoded_values: return self.decoded_values[channel]
        print("ORGretina4MModel: Error: No decoded values for channel", channel)
        print(self.decoded_values.keys())
        return None


    def max_n_rows_per_packet(self):
        return 1


    def set_object_info(self, object_info):
        self.object_info = object_info

        # parse object_info for important info
        for card_dict in self.object_info:
            crate = card_dict['Crate']
            card = card_dict['Card']

            is_enabled = card_dict['Enabled']
            ftcnt = card_dict['FtCnt']
            presum_rates = [ 2, 4, 8, 10 ] # number presummed in MS
            mrpsrt = card_dict['Mrpsrt'] # index for channel's presum rate
            dividers = [1, 2, 4, 8 ] # dividers for presummed data
            mrpsdv = card_dict['Mrpsdv'] # index for channel's divider
            for channel in range(10):
                # only care about enabled channels
                if not is_enabled[channel]: continue
                ccc = get_ccc(crate, card, channel)
                self.decoded_values[ccc] = copy.deepcopy(self.decoded_values_template)
                sd = self.decoded_values[ccc] # alias

                # find MS parameters
                # MS is on if FtCnt > 0
                # forget pre-rising-edge MS: it's broken so MJ doesn't use it
                # Skip samples at beginning, fully sample, then FtCnt samples of
                # pre-sampled, divided by div. Make one long fully-sampled wf.
                ft_len = ftcnt[channel]
                self.ft_len[ccc] = ft_len
                if self.is_multisampled(ccc):
                    ps = presum_rates[mrpsrt[channel]]
                    self.ps[ccc] = ps
                    self.div[ccc] = dividers[mrpsdv[channel]]
                    # chop off 3 values at the end because 2 are bad and we need
                    # one for interpolation
                    min_len = 2018 - ft_len - self.wf_skip + (ps-1)*(ft_len-3)
                    sd['waveform']['length'] = min_len
                    if min_len > len(self.remainders):
                        self.remainders.resize(min_len)
                else: sd['waveform']['length'] = 2016 - self.wf_skip


    def is_multisampled(self, ccc):
        if ccc in self.ft_len: return self.ft_len[ccc] > 0
        else: print('channel', ccc, 'not in ft_len...')
        return False


    def decode_packet(self, packet, lh5_tables, packet_id, header_dict, verbose=False):
        """
        Parse the header for an individual event
        """
        pu16 = np.frombuffer(packet, dtype=np.uint16)
        p16 = np.frombuffer(packet, dtype=np.int16)

        crate = (pu16[1] >> 5) & 0xF
        card = pu16[1] & 0x1F
        channel = pu16[4] & 0xf
        ccc = get_ccc(crate, card, channel)

        # aliases for brevity
        tb = lh5_tables
        if not isinstance(tb, Table):
            if ccc not in lh5_tables:
                if ccc not in self.skipped_channels:
                    self.skipped_channels[ccc] = 0
                self.skipped_channels[ccc] += 1
                return
            tb = lh5_tables[ccc]
        ii = tb.loc

        tb['packet_id'].nda[ii] = packet_id
        tb['ievt'].nda[ii] = self.ievt
        tb['energy'].nda[ii] = pu16[9] + ((pu16[10] & 0x1FF) << 16)
        tb['timestamp'].nda[ii] = pu16[6] + (pu16[7] << 16) + (pu16[8] << 32)
        tb['crate'].nda[ii] = crate
        tb['card'].nda[ii] = card
        tb['channel'].nda[ii] = channel
        tb['board_id'].nda[ii] = (pu16[4] & 0xFFF0) >> 4

        # wf starts from p16[18] and is always 2018 samples
        # we will chop off the start (~a dozen) and end (last 2), but not until
        # after we have handled multisampling
        wf = p16[18:]
        wf_len = 2018
        if self.is_multisampled(ccc):
            # get ccc pars
            ps = self.ps[ccc]
            div = self.div[ccc]
            ratio = div / ps

            # find start of presummed section
            # because it's not always self.ft_len :(
            ift = wf_len - self.ft_len[ccc] - 2
            min_diff = np.inf
            min_diff_ift = ift
            for i in range(self.wf_skip+1):
                d1 = abs(wf[ift+i]*ratio - wf[ift+i-1])
                if d1 < min_diff:
                    min_diff = d1
                    min_diff_ift = ift+i
            ift = min_diff_ift
            ift_out = ift - self.wf_skip

            # copy over fully-sampled portion
            tb['waveform']['values'].nda[ii][:ift_out] = wf[self.wf_skip:ift]

            # compute slopes for interpolation
            # rise[i-1] is the slope prior to sample i, while rise[i] is the
            # slope following it
            if len(self.rises) != len(wf): self.rises.resize(len(wf)-1)
            self.rises[:] = wf[1:] - wf[:-1]
            # correct the value right before ift
            self.rises[ift-1] = wf[ift] - np.sum(wf[ift-ps:ift]) / div

            # store double-precision values in self.remainders (later it will be
            # used to compute remainders and do round-off
            # Note: the interpolation done here is meant to preserve the
            # presumming: re-presumming should reproduce the original waveform
            rem_len = len(self.remainders) - ift_out
            for ips in range(ps):
                nn = int(np.floor( (rem_len - ips - 1) / ps ) + 1)
                self.remainders[ift_out+ips::ps] = wf[ift:ift+nn]
                self.remainders[ift_out+ips::ps] -= (2*ips+1)/(2*ps)*self.rises[ift-1:ift+nn-1]
                self.remainders[ift_out+ips::ps] *= ratio

            # now set the output based on rounding cumulative remainders
            wf_out = tb['waveform']['values'].nda[ii]
            if len(self.remainders) != len(wf_out):
                print("Error: remainders len", len(self.remainders), "but wf_out len", len(wf_out))
                return
            for ips in range(ps):
                np.rint(self.remainders[ift_out+ips::ps], out=wf_out[ift_out+ips::ps], casting='unsafe')
                if ips == ps-1: break;
                self.remainders[ift_out+ips::ps] -= wf_out[ift_out+ips::ps]
                self.remainders[ift_out+ips+1::ps] += self.remainders[ift_out+ips:-1:ps]

        else:
            length = 2016 - self.wf_skip
            start = 16+self.wf_skip
            tb['waveform']['values'].nda[ii][:] = p16[start:start+length]

        tb.push_row()

        # update the event number (searchable HDF5 column)
        self.ievt += 1


class SIS3316ORCADecoder(OrcaDecoder):
    """
    Decode ORCA Struck 3316 digitizer data.
    Thanks to J. Browning of COHERENT for getting this updated!

    TODO:
    handle per-channel data (gain, ...)
    most metadata of Struck header (energy, ...)
    """
    def __init__(self, *args, **kwargs):

        self.decoder_name = 'ORSIS3316WaveformDecoder'
        self.orca_class_name = 'ORSIS3316Model'

        # store an entry for every event
        self.decoded_values_template = {
            'packet_id': {
               'dtype': 'uint32',
             },
            'ievt': {
              'dtype': 'uint32',
            },
            'energy': {
              'dtype': 'uint32',
              'units': 'adc',
            },
            'energy_first': {
              'dtype': 'uint32',
            },
            'timestamp': {
              'dtype': 'uint64',
              'units': 'clock_ticks',
            },
            'crate': {
              'dtype': 'uint8',
            },
            'card': {
              'dtype': 'uint8',
            },
            'channel': {
              'dtype': 'uint8',
            },
            'waveform': {
              'dtype': 'uint16',
              'datatype': 'waveform',
              'length': 65532, # max value. override this before initalizing buffers to save RAM
              'sample_period': 8, # override if a different clock rate is used
              'sample_period_units': 'ns',
              'units': 'adc',
            },
        }
        super().__init__(*args, **kwargs) # also initializes the garbage df (whatever that means...)

        # self.event_header_length = 1 #?
        self.decoded_values = {}
        self.skipped_channels = {}
        self.ievt = 0


    def get_decoded_values(self, channel=None):
        if channel is None:
            dec_vals_list = self.decoded_values.items()
            if len(dec_vals_list) == 0:
                print("ORSIS3302Model: Error: decoded_values not built yet!")
                return None
            return list(dec_vals_list)[0][1] # Get first thing we find
        if channel in self.decoded_values: 
            return self.decoded_values[channel]
        print("ORSIS3316Model: Error: No decoded values for channel", channel)
        return None


    def set_object_info(self, object_info):
        self.object_info = object_info
        # parse object_info for important info
        for card_dict in self.object_info:
            crate = card_dict['Crate']
            card = card_dict['Card']
            trace_length = card_dict['rawDataBufferLen']
            import copy

            for channel in range(16):
                # get ccc and make an empty table for that ccc
                # TODO: should try and only do this for enabled channels
                ccc = get_ccc(crate, card, channel)
                self.decoded_values[ccc] = copy.deepcopy(self.decoded_values_template)

                if trace_length <= 0 or trace_length > 2**16:
                    print('SIS3316ORCADecoder Error: invalid trace_length', trace_length)
                    sys.exit()

                self.decoded_values[ccc]['waveform']['length'] = trace_length



    def max_n_rows_per_packet(self):
        return 1


    def decode_packet(self, packet, lh5_tables, packet_id, header_dict, verbose=False):

        # parse the raw event data into numpy arrays of 16 and 32 bit ints
        evt_data_32 = np.fromstring(packet, dtype=np.uint32)
        evt_data_16 = np.fromstring(packet, dtype=np.uint16)

        # tb = lh5_tables


        # read the crate/card/channel first
        crate = (evt_data_32[0] >> 21) & 0xF
        card = (evt_data_32[0] >> 16) & 0x1F
        channel = (evt_data_32[0] >> 8) & 0xFF
        ccc = get_ccc(crate, card, channel)


        if isinstance(list(lh5_tables.keys())[0], int):
            if ccc not in lh5_tables:
                print('ccc not in lh5_tables')
                if ccc not in self.skipped_channels:
                    self.skipped_channels[ccc] = 0
                self.skipped_channels[ccc] += 1
                return
            tb = lh5_tables[ccc]
        ii = tb.loc

        tb['packet_id'].nda[ii] = packet_id

        # TODO: Figure out the header, particularly card/crate/channel/timestamp
        n_lost_msb = 0
        n_lost_lsb = 0
        n_lost_records = 0
        tb['crate'].nda[ii] = evt_data_32[3]
        tb['card'].nda[ii] = evt_data_32[4]
        try:
            tb['channel'].nda[ii] = (evt_data_32[9] & 0xFFF0) >> 4
        except:
            print('Something went wrong')
            tb['channel'].nda[ii] = 33
        buffer_wrap = 0
        crate_card_chan = crate + card + channel
        wf_length_32 = 0
        ene_wf_length = evt_data_32[4]
        evt_header_id = 0
        try:
            tb['timestamp'].nda[ii] = evt_data_32[10] + ((evt_data_32[9] & 0xffff0000) << 16)
        except:
            print('something went wrong here too')
            tb['timestamp'].nda[ii] = 0

        # compute expected and actual array dimensions
        # wf_length16 = 65000 # OK this stuff is just random <--
        orca_helper_length16 = 52
        header_length16 = orca_helper_length16
        ene_wf_length16 = 2 * ene_wf_length
        footer_length16 = 0

        #expected_wf_length = (len(evt_data_16) - header_length16 - ene_wf_length16)
        expected_wf_length = (len(evt_data_16) - header_length16) #- ene_wf_length16)

        # print('expected_wf_length... ', len(evt_data_16) - header_length16)

        # if wf_length16 != expected_wf_length:
        #     print(len(evt_data_16), orca_helper_length16, header_length16,
        #          footer_length16)
        #     print("ERROR: Waveform size %d doesn't match expected size %d." %
        #       (wf_length16, expected_wf_length))
        #     print("       The Last Word (should be 0xdeadbeef):",)
        #     exit()

        # indexes of stuff (all referring to the 16 bit array)
        i_wf_start = header_length16
        #i_wf_stop = i_wf_start + wf_length16
        i_wf_stop = i_wf_start + expected_wf_length
        i_ene_start = i_wf_stop + 1
        i_ene_stop = i_ene_start + ene_wf_length16

        tbwf = tb['waveform']['values'].nda[ii]
        # handle the waveform(s)
        # if wf_length16 > 0:
        if expected_wf_length > 0:
            # print('len(tb[waveform][values].nda[ii]) ', len(tb['waveform']['values'].nda[ii]))
            # print(len(evt_data_16[i_wf_start:i_wf_stop]))
            try:
                tb['waveform']['values'].nda[ii] = evt_data_16[i_wf_start:i_wf_stop]
            except:
                print('hmm.. packet id: ', packet_id)
                print('  got: ',len(evt_data_16[i_wf_start:i_wf_stop]))
                #print(evt_data_32[i_wf_start:i_wf_stop])
                print('  expected: ',len(tb['waveform']['values'].nda[ii]))

        # TODO check if number of events matches expected
        # if len(wf_data) != expected_wf_length:
        #    print("ERROR: We expected %d WF samples and only got %d" %
        #          (expected_wf_length, len(wf_data)))
        #    exit()

        # final raw wf array

        # set the event number (searchable HDF5 column)\
        tb['ievt'].nda[ii] = self.ievt
        self.ievt += 1
        tb.push_row()


        
#This is a test decoder for the AMI286 Level sensor
class ORAMI286LEVELDECODER(OrcaDecoder):
    def __init__(self, *args, **kwargs):

        self.decoder_name = 'ORAmi286DecoderForLevel'
        self.orca_class_name = "LevelSensorDecoders"

        self.decoded_values = {
            'geoaddress': {
                'dtype': 'uint32',
            #},
            #'event_counter': {
                #'dtype': 'uint64',
            },
            'ievt': {
                'dtype': 'uint32',
            },
            'ch0_ls': {
                'dtype': 'uint32',
            },
            'ch0_timestamp': {
                'dtype': 'uint32'
            }
        }
        super().__init__(*args, **kwargs)  # also initializes the garbage df (whatever that means...)

        self.skipped_channels = {}
        self.ievt = 0

    def get_decoded_values(self, channel=None):
        # TODO: return channel-specific decoded_values
        return self.decoded_values

    def set_object_info(self, object_info):
        self.object_info = object_info

    def max_n_rows_per_packet(self):
        return 1

    def decode_packet(self, packet, lh5_tables, packet_id, header_dict, verbose=False):

        # parse the raw event data into numpy arrays of 16 and 32 bit ints
        evt_data_32 = np.fromstring(packet, dtype=np.uint32)
        evt_data_16 = np.fromstring(packet, dtype=np.uint16)

        tb = lh5_tables

        ii = tb.loc

        tb['geoaddress'].nda[ii] = (evt_data_32[0]) #& 0x7C000000) >> 26
        tb['ch0_ls'].nda[ii] = evt_data_32[1]
        tb['ch0_timestamp'].nda[ii] = evt_data_32[2]


        tb['ievt'].nda[ii] = self.ievt
        self.ievt += 1
        tb.push_row()
