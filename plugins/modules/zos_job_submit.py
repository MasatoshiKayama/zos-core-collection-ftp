from ansible.module_utils.basic import AnsibleModule
# from ansible_collections.ibm.ibm_zos_core_ftp.plugins.module_utils.job import job_output
from ..module_utils.job import job_output, job_card_contents, wait_jobs_completion
from tempfile import NamedTemporaryFile
from os import environ, path
from timeit import default_timer as timer
import re
from stat import S_IEXEC, S_IREAD, S_IWRITE
from jinja2 import Template
from ftplib import FTP
import io
from time import sleep
from six import PY2

JOB_COMPLETION_MESSAGES = ["CC", "ABEND", "SEC"]


def submit_pds_jcl(src, ftp, module):
    delete_on_close = True
    wrapper_jcl_template = """//COPYREXX EXEC PGM=IEBGENER
//SYSUT2   DD DSN=&&REXXLIB(RXPGM),DISP=(NEW,PASS),
//         DCB=(DSORG=PO,LRECL=80,RECFM=FB),
//         SPACE=(TRK,(15,,1)),UNIT=3390
//SYSPRINT DD SYSOUT=*
//SYSIN    DD DUMMY
//SYSUT1   DD *,DLM=AA
 /* REXX */
output = OUTTRAP("output.",,"CONCAT")
ADDRESS TSO "SUBMIT '{{ src }}'"
outout = OUTTRAP("OFF")
msg = Strip(output.1)
Parse Var msg msgid 'JOB ' jobname '(' jobid ')' .
say "JOBID = "jobid
AA
//* ------------------------------------------------------------------- 
//STEP0    EXEC PGM=IKJEFT01,PARM='%RXPGM'
//SYSTSPRT DD SYSOUT=*
//SYSPROC  DD DISP=(OLD,DELETE),DSN=&&REXXLIB
//SYSTSIN  DD DUMMY
"""
    wrapper_jcl = job_card_contents() + Template(wrapper_jcl_template).render({'src': src})
    delete_on_close = True
    wrapper_jcl_file = NamedTemporaryFile(delete=delete_on_close)
    with open(wrapper_jcl_file.name, 'w') as f:
        f.write(wrapper_jcl)
    with open(wrapper_jcl_file.name, 'rb') as f:
        stdout = ftp.storlines("STOR JCL", f)
    wrapper_jcl_jobId = re.search(r'JOB\d{5}', stdout).group()
 
    # Wait for the complation of the original job
    original_job_wait_time_s = 20
    duration = wait_jobs_completion(ftp, wrapper_jcl_jobId, original_job_wait_time_s)
    if duration > original_job_wait_time_s:
       raise SubmitJCLError(
            "The job can not be queried from JES (Timeout=10s). Please check the zOS system.  It is slow to respond."
        )

    # Get the jobid of the original job
    jobId = ""
    joblog = []
    ftp.retrlines("RETR " + wrapper_jcl_jobId, joblog.append)
    for line in joblog:
        if re.search(r'JOBID = JOB\d{5}', line):
            jobId = re.search(r'JOB\d{5}', line).group()
            break

    if jobId == "":
        raise SubmitJCLError("SUBMIT JOB FAILED:  jobId :" + wrapper_jcl_jobId)
    return jobId

def submit_jcl_in_volume(src, vol, ftp, module):
    return submit_uss_jcl(src, ftp, module)

def submit_ftp_jcl(src, ftp, module):
    delete_on_close = True
    with open(src, "rb") as f:
        stdout = ftp.storlines("STOR " + src, f)
    jobId = re.search(r'JOB\d{5}', stdout).group()
    return jobId

def get_job_info(ftp, module, jobId, return_output):
    result = dict()
    try:
        result["jobs"] = query_jobs_status(ftp, module, jobId)
    except SubmitJCLError:
        raise

    if not return_output:
        for job in result.get("jobs", []):
            job["ddnames"] = []

    result["changed"] = True

    return result

def query_jobs_status(ftp, module, jobId):
    timeout = 20
    output = job_output(ftp, job_id=jobId)
    while not output and timeout > 0:
        try:
            sleep(0.5)
            output = job_output(ftp, job_id=jobId)
            timeout = timeout - 1
        except IndexError:
            pass
        except Exception as e:
            raise SubmitJCLError(
                "{0} {1} {2}".format(repr(e), "The output is: ", output or " ")
            )
    if not output and timeout == 0:
        raise SubmitJCLError(
            "The job can not be queried from JES (Timeout=10s). Please check the zOS system.  It is slow to respond."
        )
    return output


def assert_valid_return_code(max_rc, found_rc):
    if found_rc is None or max_rc < int(found_rc):
        raise SubmitJCLError("")


def run_module():
    module_args = dict(
        src=dict(type="str", required=True),
        wait=dict(type="bool", required=False),
        location=dict(
            type="str",
            default="DATA_SET",
            choices=["DATA_SET", "USS", "LOCAL"],
        ),
        volume=dict(type="str", required=False),
        return_output=dict(type="bool", required=False, default=True),
        wait_time_s=dict(type="int", default=60),
        max_rc=dict(type="int", required=False)
    )

    module = AnsibleModule(argument_spec=module_args, supports_check_mode=True)
    result = dict(
        changed=False,
    )

    location = module.params["location"]
    volume = module.params["volume"]
    wait = module.params["wait"]
    src = module.params["src"]
    return_output = module.params["return_output"]
    wait_time_s = module.params["wait_time_s"]
    max_rc = module.params["max_rc"]

    if wait_time_s <= 0:
        module.fail_json(
            msg="The option wait_time_s is not valid.  It must be greater than 0.",
            **result
        )

    if environ.get('FTP_SOCKS_PORT'):
       import socks
       import socket
       socks.set_default_proxy(socks.SOCKS5, "127.0.0.1", int(environ.get('FTP_SOCKS_PORT')))
       socket.socket = socks.socksocket

    try:
       if environ.get('FTP_TLS_VERSION'):
           from ftplib import FTP_TLS
           import ssl
           cert_file_path = environ.get('FTP_TLS_CERT_FILE')
           if cert_file_path:
               if not path.isfile(cert_file_path):
                   module.fail_json(
                       msg="The TLS cartificate file not found: {0}".format(repr(cert_file_path)), **result
                   )
               context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
               context.load_verify_locations(cert_file_path)
               context.check_hostname = False
               ftp = FTP_TLS(context=context)
           else:
               ftp = FTP_TLS()
           tls_version = environ.get('FTP_TLS_VERSION')
           if tls_version == '1.2':
               ftp.ssl_version = ssl.PROTOCOL_TLSv1_2
       else:
           ftp = FTP()
       ftp.connect(environ.get('FTP_HOST'), int(environ.get('FTP_PORT') or 21))
       ftp.login(environ.get('FTP_USERID'), environ.get('FTP_PASSWORD'))
       ftp.sendcmd("site filetype=jes")
       ftp.set_pasv(True)

       if environ.get('FTP_TLS_VERSION'):
           ftp.prot_p()

    except Exception as e:
       module.fail_json(
           msg="An unexpected error occurred during FTP login: {0}".format(repr(e)), **result
       )

    DSN_REGEX = r"^(?:(?:[A-Z$#@]{1}[A-Z0-9$#@-]{0,7})(?:[.]{1})){1,21}[A-Z$#@]{1}[A-Z0-9$#@-]{0,7}(?:\([A-Z$#@]{1}[A-Z0-9$#@]{0,7}\)){0,1}$"
    try:
        if location == "DATA_SET":
            data_set_name_pattern = re.compile(DSN_REGEX, re.IGNORECASE)
            if PY2:
                check = data_set_name_pattern.match(src)
            else:
                check = data_set_name_pattern.fullmatch(src)
            if check:
                if volume is None or volume == "":
                    jobId = submit_pds_jcl(src, ftp, module)
                else:
                    jobId = submit_jcl_in_volume(src, volume, ftp, module)
            else:
                ftp.quit()
                module.fail_json(
                    msg="The parameter src for data set is not a valid name pattern. Please check the src input.",
                    **result
                )
        else:
            jobId = submit_ftp_jcl(src, ftp, module)

    except SubmitJCLError as e:
        module.fail_json(msg=repr(e), **result)
    if jobId is None or jobId == "":
        result["job_id"] = jobId
        ftp.quit()
        module.fail_json(
            msg="JOB ID Returned is None. Please check whether the JCL is valid.",
            **result
        )

    result["job_id"] = jobId
    if not wait:
        wait_time_s = 10

    try:
        duration = wait_jobs_completion(ftp, jobId, wait_time_s)
    except SubmitJCLError as e:
        ftp.quit()
        module.fail_json(msg=repr(e), **result)

    if duration >= wait_time_s:
        result["message"] = {
            "stdout": "Submit JCL operation succeeded but it is a long running job. Timeout is "
            + str(wait_time_s)
            + " seconds.  JobID is "
            + str(jobId)
            + "."
        }

    try:
        result = get_job_info(ftp, module, jobId, return_output)
        ftp.quit()
        if wait is True and return_output is True and max_rc is not None:
            assert_valid_return_code(
                max_rc, result.get("jobs")[0].get("ret_code").get("code")
            )
    except SubmitJCLError as e:
        ftp.quit()
        module.fail_json(msg=repr(e), **result)
    except Exception as e:
        ftp.quit()
        module.fail_json(msg=repr(e), **result)

    result["duration"] = duration
    result["message"] = {"stdout": "Submit JCL operation succeeded."}
    result["changed"] = True
    module.exit_json(**result)


class Error(Exception):
    pass


class SubmitJCLError(Error):
    def __init__(self, jobs):
        self.msg = 'An error occurred during submission of jobs "{0}"'.format(jobs)


def main():
    run_module()


if __name__ == "__main__":
    main()

