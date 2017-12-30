import imaplib
import socket
import ssl
import getpass
import pprint
import email
import re
import urllib.request
from bs4 import BeautifulSoup
from datetime import datetime
from enum import Enum
from astroquery.ned import Ned
import astropy.units as u
from astropy import coordinates
import numpy as np
from astroquery.irsa_dust import IrsaDust
import datetime

reg_obj = b"https://wis-tns.weizmann.ac.il/object/(\w+)"
#reg_ra = b"\d{4}\w+\sRA[\=a-zA-Z\<\>\" ]+(\d{2}:\d{2}:\d{2}\.\d+)"
#reg_dec = b"DEC[\=a-zA-Z\<\>\" ]+((?:\+|\-)\d{2}:\d{2}:\d{2}\.\d+)\,\s\w+"

reg_ra = b"\>\sRA[\=\*a-zA-Z\<\>\" ]+(\d{2}:\d{2}:\d{2}\.\d+)"
reg_dec = b"DEC[\=\*a-zA-Z\<\>\" ]+((?:\+|\-)\d{2}:\d{2}:\d{2}\.\d+)\<\/em\>\,"

def try_parse_float(s):
    try:
        test = float(s)
        return True
    except ValueError:
        return False

class phot_row:
    def __init__(self, rowResultSet):
        
        self.ID = None
        self.Obs_date = None
        self.Mag_Flux = None
        self.Err = None
        self.Lim_Mag_Flux = None
        self.Units = None
        self.Filter = None 
        self.Tel_Inst = None 
        self.Exp_time = None
        self.Observers = None 
        self.Remarks = None
        
        tds = rowResultSet.find_all('td')
        
        if len(tds) > 0:
            self.ID = tds[0].text
            self.Obs_date = datetime.datetime.strptime(tds[1].text, '%Y-%m-%d %H:%M:%S') if tds[1].text != '' else None
            self.Mag_Flux = float(tds[2].text) if tds[2].text != '' else None
            self.Err = float(tds[3].text) if tds[3].text != '' else None
            self.Lim_Mag_Flux = float(tds[4].text) if tds[4].text != '' else None
            self.Units = tds[5].text
            self.Filter = tds[6].text
            self.Tel_Inst = tds[7].text
            self.Exp_time = tds[8].text
            self.Observers = tds[9].text
            self.Remarks = tds[10].text

class ned_row:
    def __init__(self,detail_url, obj_name, ra, dec, object_type, z, separation):
        self.detail_url = detail_url
        self.obj_name = obj_name
        self.ra = ra
        self.dec = dec
        self.object_type = object_type
        self.z = z
        self.separation = separation

class tns_obj:
    def __init__(self, name, tns_url, internal_name, event_type, ra, dec, a_v, z, tns_host, tns_host_z, \
                 ned_nearest_host, ned_nearest_z, ned_nearest_sep, discovery_date, phot_rows, disc_mag):

        self.Name = name
        self.TNS_URL = tns_url
        self.Internal_Name = internal_name
        self.Event_Type = event_type if (event_type != '---' and event_type != '') else "Untyped"
        self.Ra = ra
        self.Dec = dec
        self.A_v = np.asarray([float(a) for a in a_v])
        self.Z = float(z) if (z != '---' and z != '') else -1
        self.TNS_Host = tns_host
        self.TNS_Host_Z = float(tns_host_z) if (tns_host_z != '---' and tns_host_z != '') else -1
        self.NED_Nearest_host = ned_nearest_host
        self.NED_Nearest_z = np.asarray([round(float(hz),3) for hz in ned_nearest_z])
        self.NED_Nearest_sep = np.asarray([float(s) for s in ned_nearest_sep])
        self.Discovery_Date = datetime.datetime.strptime(discovery_date, '%Y-%m-%d %H:%M:%S')
        self.Photometry = phot_rows
        self.Disc_Mag = float(disc_mag)
        
        dec_deg = float(dec[:3])
        self.Survey = "FOUNDATION" if dec_deg > -30 else "SWOPE"

    # Get the last photometry row that has the remark "Last non detection". If this doens't exist, just get the
    # last photometry row.
    def last_nondetection(self):
        ln_dates = [p for p in self.Photometry if "Last non detection" in p.Remarks]
        if ln_dates is None or len(ln_dates) == 0:
            ln_dates = self.Photometry

        lnd = ln_dates[-1].Obs_date
        days = (datetime.datetime.today() - lnd).days
        
        return (lnd,days)
    
    def build_comments(self):
        
        comments = []
        lnd = self.last_nondetection()

        #.decode("utf-8")
        if not np.any([(h == self.TNS_Host) for h in self.NED_Nearest_host]):
            comments.append("-TNS and NED hosts disagree.")

        if not (np.any(self.NED_Nearest_z == self.TNS_Host_Z) or np.any(self.NED_Nearest_z == self.Z)):
            comments.append("-TNS and NED z disagree.")

        if (lnd[1] >= 17 and lnd[1] <= 20):
            comments.append("-Last non-detection questionable.")

        return comments
        
    def validate(self):
        
        bad_reasons = []
        
        # Last non-detection check
        lnd = self.last_nondetection()
        good_lnd = (lnd[1] <= 20)
        
        # Reddening check
        good_r = (self.A_v is not None and len(self.A_v) > 0 and not np.any(self.A_v >= 0.5))
        
        # Host check - exists?
        good_h = (self.TNS_Host != "") or (self.NED_Nearest_host is not None or len(self.NED_Nearest_host) > 0)
        
        # Z check - either obj z, host z, or NED z exists and is in range
        good_obj_z = False
        good_TNS_host_z = False
        good_NED_z = False
        
        # if obj_z is present, it trumps the rest of the checks.
        #  if good, stop checks
        #  if bad, stop checks and report
        #
        # if obj_z is missing, check host_z
        #   if host_z is present, it trumps the rest of checks
        #     if good, stop checks
        #     if bad, stop checks and report
        #
        # if NED_z is present,
        #   if bad, report
        # *If any z is less than 0.015, mark as questionable. Might be close enough

        obj_z_present = (self.Z is not None and self.Z != "" and self.Z != -1)
        tns_host_z_present = (self.TNS_Host_Z is not None and self.TNS_Host_Z != "" and self.TNS_Host_Z != -1)

        if obj_z_present:
            good_obj_z = (self.Z >= 0.015 and self.Z <= 0.08)
        
        if tns_host_z_present:
            good_TNS_host_z = (self.TNS_Host_Z >= 0.015 and self.TNS_Host_Z <= 0.08)
        
        good_NED_z = np.any([(nedz >= 0.015 and nedz <= 0.08) for nedz in self.NED_Nearest_z])

        # Questionable if too low of z, or LND same as discovery date
        questionable = ((self.Z > 0.0 and self.Z < 0.015) or \
                        (self.TNS_Host_Z  > 0.0 and self.TNS_Host_Z < 0.015) or \
                        np.any([(nedz < 0.015) for nedz in self.NED_Nearest_z]) or \
                        lnd[1] == 0)

        good_z = (good_obj_z or good_TNS_host_z or good_NED_z)
            
        # check obj type - either unspecified of SN Ia
        good_type = (self.Event_Type == "" or self.Event_Type == "Untyped" or self.Event_Type == "SN Ia")
        
        # ---- Checks ---- #
        if not good_lnd:
            bad_reasons.append("-Last non-detection too long: %s" % lnd[1])
        
        if not good_r:
            av = -1
            if len(self.A_v) > 0:
                av = self.A_v[0]
            bad_reasons.append("-Reddening too high or not available; a_v = %s" % av)
        
        if not good_h:
            bad_reasons.append("-No host in TNS or NED")

        if (obj_z_present):
            if (not good_obj_z):
                bad_reasons.append("-Bad TNS Object z: %s" % self.Z)
        else:
            if (tns_host_z_present):
                if (not good_TNS_host_z):
                    bad_reasons.append("-No TNS Object z & bad TNS Host z: %s" % self.TNS_Host_Z)
            else:
                if (not good_NED_z):
                    bad_reasons.append("-No TNS Object or Host z, no good NED z")
        
        if not good_type:
            bad_reasons.append("-Object not 'Untyped' or 'SN Ia'")
        
        valid = (good_lnd and good_r and good_h and good_z and good_type)
        
        status = "Questionable"
        if not questionable:
            if valid:
                status = "True"
            else:
                status = "False"    
        
        return (status, bad_reasons)

def WriteLine(f,text,em):    
    line = text
    if em and text != "":
        line = "*"+text+"*"

    f.write(line+"\n")

def WriteOutput(tns_objs):
    today = datetime.datetime.today().strftime('%Y%m%d_%H%M')
    fname1 = ("TNS_Report_%s.txt" % today)

    with open(fname1, 'w') as f:
        for i in range(len(tns_objs)):

            sindex = np.argsort(tns_objs[i].NED_Nearest_sep)
            val = tns_objs[i].validate()
            em = (val[0] == "True" or val[0] == "Questionable")

            WriteLine(f,"--------------------------------------------------------",em)
            WriteLine(f,("Valid?: %s" % val[0]),em)
            WriteLine(f,"Reason(s):",em)
            [WriteLine(f,r,em) for r in val[1]]
            WriteLine(f,("Survey: %s" % tns_objs[i].Survey),em)
            WriteLine(f,("Name: %s (%s)" % (tns_objs[i].Name, tns_objs[i].TNS_URL)),em)
            WriteLine(f,("Internal Name: %s" % tns_objs[i].Internal_Name),em)
            WriteLine(f,("Type: %s" % tns_objs[i].Event_Type),em)
            WriteLine(f,("Dec: %s" % tns_objs[i].Dec),em)
            WriteLine(f,("Host (via TNS): %s" % tns_objs[i].TNS_Host),em)
            WriteLine(f,("Object z (via TNS): %s" % tns_objs[i].Z),em)
            WriteLine(f,("Host z (via TNS): %s" % tns_objs[i].TNS_Host_Z),em)
            WriteLine(f,("Disc. Date: %s" % tns_objs[i].Discovery_Date),em)
            
            lnd = tns_objs[i].last_nondetection()
            WriteLine(f,("Last non-detection: %s" % lnd[0]),em)
            WriteLine(f,("Last non-detection days: %s" % lnd[1]),em)
            WriteLine(f,("Disc. Mag: %s" % tns_objs[i].Disc_Mag),em)
            WriteLine(f,"",em)
            WriteLine(f,"--NED info--",em)
            if len(tns_objs[i].NED_Nearest_host) > 0:
                for j in range(len(tns_objs[i].NED_Nearest_host)):

                    formatted_text = ("NED Host: %s; Separation [arcmin]: %s; a_v: %s; z: %s" % \
                                       (tns_objs[i].NED_Nearest_host[sindex[j]], #.decode("utf-8")
                                        tns_objs[i].NED_Nearest_sep[sindex[j]],
                                        tns_objs[i].A_v[sindex[j]],
                                        tns_objs[i].NED_Nearest_z[sindex[j]]
                                       ))
                    WriteLine(f,formatted_text,em)
            WriteLine(f,"",em)
            WriteLine(f,"Comment(s):",em)
            [WriteLine(f,c,em) for c in tns_objs[i].build_comments()]
    
    with open("TNS_Outputs.txt", 'w') as t_o:
        t_o.write(fname1)

class processTNS():
    def __init__(self):
        self.verbose = None

    def add_options(self, parser=None, usage=None, config=None):
        import optparse
        if parser == None:
            parser = optparse.OptionParser(usage=usage, conflict_handler="resolve")

        # The basics
        parser.add_option('-v', '--verbose', action="count", dest="verbose",default=1)
        parser.add_option('--clobber', default=False, action="store_true",
                          help='clobber output file')
        parser.add_option('-s','--settingsfile', default=None, type="string",
                          help='settings file (login/password info)')

        if config:
            parser.add_option('--login', default=config.get('main','login'), type="string",
                              help='gmail login (default=%default)')
            parser.add_option('--password', default=config.get('main','password'), type="string",
                              help='gmail password (default=%default)')
        else:
            parser.add_option('--login', default="", type="string",
                              help='gmail login (default=%default)')
            parser.add_option('--password', default="", type="string",
                              help='gmail password (default=%default)')
        return(parser)

    def ProcessTNSEmails(self):
        body = ""
        html = ""
        tns_objs = []
        radius = 5 # arcminutes

        try:
        
            ########################################################
            # Get All Email
            ########################################################
        
            mail =  imaplib.IMAP4_SSL('imap.gmail.com', 993) #, ssl_context=ctx
        
            ## NOTE: This is not the way to do this. You will want to implement an industry-standard login step ##
            mail.login(self.login, self.password)

            mail.select('TNS', readonly=False)
            retcode, msg_ids_bytes = mail.search(None, '(UNSEEN)')
            msg_ids = msg_ids_bytes[0].decode("utf-8").split(" ")

            if retcode != "OK" or msg_ids[0] == "":
                raise ValueError("No messages")

            for i in range(len(msg_ids)):
                ########################################################
                # Iterate Over Email
                ########################################################
                typ, data = mail.fetch(msg_ids[i],'(RFC822)')
                # Mark messages as "Seen"
                result, wdata = mail.store(msg_ids[i], '+FLAGS', '\\Seen')
                msg = email.message_from_bytes(data[0][1])

                if msg.is_multipart():
                    for part in msg.walk():
                        ctype = part.get_content_type()
                        cdispo = str(part.get('Content-Disposition'))

                        # skip any text/plain (txt) attachments
                        if ctype == 'text/plain' and 'attachment' not in cdispo:
                            body = part.get_payload(decode=True)  # decode
                            break
                # not multipart - i.e. plain text, no attachments, keeping fingers crossed
                else:
                    body = msg.get_payload(decode=True)

                objs = re.findall(reg_obj,body)
                print(objs)
                ras = re.findall(reg_ra,body)
                print(ras)
                decs = re.findall(reg_dec,body)
                print(decs)
            
                ########################################################
                # For Item in Email, Get TNS
                ########################################################
            
                for j in range(len(objs)):
                    print("Object: %s\nRA: %s\nDEC: %s" % (objs[j].decode('utf-8'),
                                                           ras[j].decode('utf-8'),
                                                           decs[j].decode('utf-8')))
                
                    # Get TNS page
                    int_name=""
                    evt_type=""
                    z=""
                    host_name=""
                    host_redshift = ""
                    ned_url = ""
            
                    tns_url = "https://wis-tns.weizmann.ac.il/object/" + objs[j].decode("utf-8")
                    print(tns_url)
                    with urllib.request.urlopen(tns_url) as tns:
                        html = tns.read()
                        soup = BeautifulSoup(html, "lxml")
                    
                        # Get Internal Name, Type, Disc. Date, Disc. Mag, Redshift, Host Name, Host Redshift, NED URL
                        int_name = soup.find('td', attrs={'class':'cell-internal_name'}).text
                        evt_type = soup.find('div', attrs={'class':'field-type'}).find('div').find('b').text
                        disc_date = soup.find('div', attrs={'class':'field field-discoverydate'}).find('div').find('b').text
                        disc_mag = soup.find('div', attrs={'class':'field field-discoverymag'}).find('div').find('b').text
                        z = soup.find('div', attrs={'class':'field-redshift'}).find('div').find('b').text
                    
                        hn_div = soup.find('div', attrs={'class':'field-hostname'})
                        if hn_div is not None:
                            host_name = hn_div.find('div').find('b').text

                        z_div = soup.find('div', attrs={'class':'field-host_redshift'})
                        if z_div is not None:
                            host_redshift = z_div.find('div').find('b').text

                        ned_url = soup.find('div', attrs={'class':'additional-links clearfix'}).find('a')['href']
                    
                        # Get photometry records
                        table = soup.findAll('table', attrs={'class':'photometry-results-table'})
                        prs = []
                        for k in range(len(table)):

                            table_body = table[k].find('tbody')
                            rows = table_body.find_all('tr')
                            print(type(rows))

                            for l in range(len(rows)):
                                prs.append(phot_row(rows[l]))
                    
                    ########################################################
                    # For Item in Email, Get NED
                    ########################################################
                    ra_j = ras[j].decode("utf-8")
                    dec_j = decs[j].decode("utf-8")
            
                    co = coordinates.SkyCoord(ra=ra_j, dec=dec_j, unit=(u.hour, u.deg), frame='fk4', equinox='J2000.0')
                    ned_region_table = None
                
                    gal_candidates = 0
                    radius = 5
                    while (radius < 11 and gal_candidates < 21): 
                        try:
                            print("Radius: %s" % radius)
                            ned_region_table = Ned.query_region(co, radius=radius*u.arcmin, equinox='J2000.0')
                            gal_candidates = len(ned_region_table)
                            radius += 1
                            print("Result length: %s" % gal_candidates)
                        except Exception as e:
                            radius += 1
                            print("NED exception: %s" % e.args)

                    galaxy_names = []
                    galaxy_zs = []
                    galaxy_seps = []
                    galaxies_with_z = []
                    a_vs = []
                    if ned_region_table is not None:
                        print("NED Matches: %s" % len(ned_region_table))

                        galaxy_candidates = np.asarray([entry.decode("utf-8") for entry in ned_region_table["Type"]])
                        galaxies_indices = np.where(galaxy_candidates == 'G')
                        galaxies = ned_region_table[galaxies_indices]
                        
                        print("Galaxy Candidates: %s" % len(galaxies))

                        # Get Galaxy name, z, separation for each galaxy with z
                        for l in range(len(galaxies)):
                            if isinstance(galaxies[l]["Redshift"], float):
                                galaxies_with_z.append(galaxies[l])
                                galaxy_names.append(galaxies[l]["Object Name"])
                                galaxy_zs.append(galaxies[l]["Redshift"])
                                galaxy_seps.append(galaxies[l]["Distance (arcmin)"])


                        print("Galaxies with z: %s" % len(galaxies_with_z))
                        # Get Dust in LoS for each galaxy with z
                        if len(galaxies_with_z) > 0:
                            for l in range(len(galaxies_with_z)):
                                co_l = coordinates.SkyCoord(ra=galaxies_with_z[l]["RA(deg)"], 
                                                            dec=galaxies_with_z[l]["DEC(deg)"], 
                                                            unit=(u.deg, u.deg), frame='fk4', equinox='J2000.0')

                                dust_table_l = IrsaDust.get_extinction_table(co_l)

                                a_vs.append(dust_table_l["A_SandF"][np.where(dust_table_l["Filter_name"] == "CTIO V")][0])
                        else:
                            print("No NED Galaxy hosts with z")

                    tns_objs.append(tns_obj(name = objs[j].decode("utf-8"),
                                            tns_url = tns_url,
                                            internal_name = int_name,
                                            event_type = evt_type,
                                            ra = ras[j].decode("utf-8"),
                                            dec = decs[j].decode("utf-8"),
                                            a_v = a_vs,
                                            z = z,
                                            tns_host = host_name, 
                                            tns_host_z = host_redshift,
                                            ned_nearest_host = galaxy_names, 
                                            ned_nearest_z = galaxy_zs,
                                            ned_nearest_sep = galaxy_seps,
                                            discovery_date = disc_date,
                                            phot_rows = prs, 
                                            disc_mag = disc_mag
                                            ))

        except ValueError as err:
            print("%s. Exiting..." % err.args)
            mail.close()
            mail.logout()
            del mail

        WriteOutput(tns_objs)
        print("Process done.")


if __name__ == "__main__":
    # execute only if run as a script

    import optparse
    import configparser

    usagestring = "TNS_Synopsis.py <options>"
    
    tnsproc = processTNS()

    # read in the options from the param file and the command line
    # some convoluted syntax here, making it so param file is not required
    parser = tnsproc.add_options(usage=usagestring)
    options,  args = parser.parse_args()
    if options.settingsfile:
        config = configparser.ConfigParser()
        config.read(options.settingsfile)
    else: config=None
    parser = tnsproc.add_options(usage=usagestring,config=config)
    options,  args = parser.parse_args()

    tnsproc.login = options.login
    tnsproc.password = options.password
    tnsproc.ProcessTNSEmails()
