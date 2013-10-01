import time
import requests
import json
import re
import zipfile
import io
import tempfile, os.path, subprocess
from django.conf import settings
from django.dispatch import receiver
from django.core.files import File
from django.core.files.base import ContentFile

import celery
from celery.signals import task_success, task_failure, task_revoked, task_sent
from codalab.settings import base

from azure.storage import *

import models

main = base.Base.SITE_ROOT

def local_run(url, submission_id):
    """
        This routine will take the job (initially a competition submission, later a run) and execute it locally.
    """
    submission = models.CompetitionSubmission.objects.get(pk=submission_id)
    program = submission.phase.scoring_program.name
    dataset = submission.phase.reference_data.name
    print "Running locally"
    base_dir = models.submission_root(submission)
    
    # Make a directory for the run
    job_dir = os.path.join(tempfile.gettempdir(), base_dir)
    print "Job Dir: %s" % job_dir
    if not os.path.exists(job_dir):
        os.makedirs(job_dir)
    else:
        print "Job dir already exists, clearing it out."
        os.rmdir(job_dir)
        os.makedirs(job_dir)

    input_dir = os.path.join(job_dir, "input")
    output_dir = os.path.join(job_dir, "output")
    program_dir = os.path.join(job_dir, "program")

    # Make the run/output directory
    for d in [os.path.join(input_dir, "ref"), os.path.join(input_dir, "res"), program_dir, output_dir]:
        os.makedirs(d)

    # Grab the program bundle, unpack it in the run/program directory
    pzip = zipfile.ZipFile(io.BytesIO(submission.phase.scoring_program.read()))
    pzip.extractall(os.path.join(job_dir, "program"))
    metadata = open(os.path.join(program_dir, "metadata")).readlines()
    for line in metadata:
        print line
        key, value = line.split(":")
        if "command" in key.lower():
            for cmdterm in value.split(" "):
                if "$program" in cmdterm:
                    prefix, cmd = cmdterm.split("/")
                    print cmd
    command = os.path.join(program_dir, cmd)

    # Grab the reference bundle, unpack in the run/input directory
    rzip = zipfile.ZipFile(io.BytesIO(submission.phase.reference_data.read()))
    rzip.extractall(os.path.join(job_dir, "input", "ref"))

    # Grab the submission bundle, unpack it in the directory
    szip = zipfile.ZipFile(io.BytesIO(submission.file.read()))
    szip.extractall(os.path.join(job_dir, "input", "res"))

    # Execute the job
    stdout_file = open(os.path.join(output_dir, "stdout.txt"), 'wb')
    stderr_file = open(os.path.join(output_dir, "stderr.txt"), 'wb')
    subprocess.call([command, input_dir, output_dir], stdout=stdout_file, stderr=stderr_file)

    # Pack up the output and store it in Azure.

@celery.task(name='competition.submission_run')
def submission_run(url,submission_id):

    if 'local' in settings.COMPUTATION_SUBMISSION_URL:
        local_run(url, submission_id)
    else:
        time.sleep(0.01) # Needed temporarily for using sqlite. Race.

        submission = models.CompetitionSubmission.objects.get(pk=submission_id)
        program = submission.phase.scoring_program.name
        dataset = submission.phase.reference_data.name

        # Generate input bundle pointing to reference/truth/gold dataset (ref) and user predictions (res).
        inputfile = ContentFile(
    """ref: %s
    res: %s
    """ % (dataset, submission.file.name))   
        submission.inputfile.save('input.txt', inputfile)
        # Generate run bundle, which binds the input bundle to the scoring program
        runfile = ContentFile(
    """program: %s
    input: %s
    """ % (program, submission.inputfile.name))
        submission.runfile.save('run.txt', runfile)
        # Log start of evaluation to stdout.txt
        stdoutfile = ContentFile(
    """Standard output file for submission #%s:

    """ % (submission.submission_number))
        submission.stdout_file.save('run/stdout.txt', stdoutfile)
        submission.save()
        # Submit the request to the computation service
        headers = {'content-type': 'application/json'}
        data = json.dumps({ "RunId" : submission.runfile.name, "Container" : settings.BUNDLE_AZURE_CONTAINER })
        res = requests.post(settings.COMPUTATION_SUBMISSION_URL, data=data, headers=headers)
        print "submitting: %s" % submission.runfile.name
        if res.status_code in (200,201):
            data = res.json()
            submission.execution_key = data['Id']
            submission.set_status(models.CompetitionSubmissionStatus.SUBMITTED)
        else:
            submission.set_status(models.CompetitionSubmissionStatus.FAILED)
        submission.save()
        submission_get_results.delay(submission.pk,1)
        return submission.pk

@celery.task(name='competition.submission_get_results')
def submission_get_results(submission_id,ct):
    # TODO: Refactor
    # Hard-coded limits for now
    submission = models.CompetitionSubmission.objects.get(pk=submission_id)
    if ct > 1000:
        # return None to indicate bailing on checking
        return (submission.pk,ct,'limit_exceeded',None)
    # Get status of computation from the computation engine
    status = submission.get_execution_status()
    print "Computation status: %s" % str(status)
    if status:
        if status['Status'] in ("Submitted"):
            submission.set_status(models.CompetitionSubmissionStatus.SUBMITTED, force_save=True)
            return (submission.pk, ct+1, 'rerun', None)
        if status['Status'] in ("Running"):
            submission.set_status(models.CompetitionSubmissionStatus.RUNNING, force_save=True)
            return (submission.pk, ct+1, 'rerun', None)
        elif status['Status'] == "Finished":
            submission.set_status(models.CompetitionSubmissionStatus.FINISHED, force_save=True)
            return (submission.pk, ct, 'complete', status)
        elif status['Status'] == "Failed":
            submission.set_status(models.CompetitionSubmissionStatus.FAILED, force_save=True)
            return (submission.pk, ct, 'failed', status)
    else:
        return (submission.pk,ct,'failure',None)
    
@task_success.connect(sender=submission_get_results)
def submission_results_success_handler(sender,result=None,**kwargs):
    submission_id,ct,state,status = result
    submission = models.CompetitionSubmission.objects.get(pk=submission_id)
    if state == 'rerun':
        print "Querying for results again"
        submission_get_results.apply_async((submission_id,ct),countdown=5)
    elif state == 'complete':
        print "Run is complete (submission.id: %s)" % submission.id
        submission.output_file.name = models.submission_file_blobkey(submission)
        submission.stderr_file.name = models.submission_stderr_filename(submission)
        submission.save()
        print "Retrieving output.zip and 'scores.txt' file"
        ozip = zipfile.ZipFile(io.BytesIO(submission.output_file.read()))
        scores = open(ozip.extract('scores.txt'), 'r').read()
        print "Processing scores..."
        for line in scores.split("\n"):
            if len(line) > 0:
                label, value = line.split(":")
                try:
                    scoredef = models.SubmissionScoreDef.objects.get(competition=submission.phase.competition,  key=label.strip())
                    models.SubmissionScore.objects.create(result=submission, scoredef=scoredef, value=float(value))                    
                except models.SubmissionScoreDef.DoesNotExist as e:
                    print "Score %s does not exist" % label
                    pass
        print "Done processing scores..."
    elif state == 'limit_exceeded':
        print "Run limit, or time limit exceeded."
        raise Exception("Computation exceeded its allotted time quota.")
    else:
        raise Exception("An unexpected error has occurred.")

@task_success.connect(sender=submission_run)
def submission_run_success_handler(sender, result=None, **kwargs):
    print "Successful submission"

@task_failure.connect(sender=submission_run)
def submission_run_error_handler(sender, exception=None, **kwargs):
    submission_id = kwargs['kwargs']['submission_id']
    print "Handling failure for submission %s" % submission_id
    try:
        submission = models.CompetitionSubmission.objects.get(pk=submission_id)
        submission.set_status(models.CompetitionSubmissionStatus.FAILED, force_save=True)
    except:
        print "Unable to set Failed state of submission %s" % submission_id

# Bundle Tasks
@celery.task
def create_directory(bundleid):
    import subprocess
    bundle = models.Bundle.objects.get(id=bundleid)
    args = ['cd repositories && mkdir -p '+ bundle.name]
    subprocess.check_output(args, shell=True)
    bundle.path = main+'/repositories/'+bundle.name
    bundle.save()
    print bundle.path
    sub_directories.delay(bundleid)


@celery.task
def sub_directories(bundleid):
    import subprocess
    bundle = models.Bundle.objects.get(id=bundleid)
    args = ['cd repositories/'+bundle.name+' && mkdir -p input && mkdir -p output']
    subprocess.check_output(args, shell=True)
    bundle.inputpath = bundle.path+'/input'
    bundle.outputpath = bundle.path+'/output'
    bundle.save()
    print "The directories have been created"
    args = ['cd '+bundle.path+' && touch bundle.yaml']
    subprocess.check_output(args, shell=True)
    bundle.save()
    print "The bundle yaml has been created"
