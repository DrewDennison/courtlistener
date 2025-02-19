from datetime import date

import pandas as pd

from cl.corpus_importer.import_columbia.parse_opinions import (
    get_state_court_object,
)
from cl.people_db.import_judges.judge_utils import (
    get_school,
    get_suffix,
    process_date,
)
from cl.people_db.models import (
    Education,
    Person,
    PoliticalAffiliation,
    Position,
    Source,
)


def make_state_judge(item, testing=False):
    """Takes the state judge data <item> and associates it with a Judge object.

    Saves the judge to the DB.
    """

    if pd.isnull(item["startyear"]):
        return

    date_dob, date_granularity_dob = process_date(
        item["birthyear"], item["birthmonth"], item["birthday"]
    )
    date_dod, date_granularity_dod = process_date(
        item["deathyear"], item["deathmonth"], item["deathday"]
    )

    if item["firstname"] == "":
        return
    if not pd.isnull(item["midname"]):
        if len(item["midname"]) == 1:
            item["midname"] = item["midname"] + "."

    had_alias_result = False
    check = Person.objects.filter(
        name_first=item["firstname"],
        name_last=item["lastname"],
        date_dob=date_dob,
    )
    name = "%s: %s %s %s" % (
        item["cl_id"],
        item["firstname"],
        item["lastname"],
        date_dob,
    )
    if check.count() > 0:
        print("Warning: %s exists." % name)
        person = check[0]
        if person.is_alias:
            # Grab the correct person and set our alias variable to True
            person = person.is_alias_of
    else:
        print("Now processing: %s" % name)

        person = Person(
            gender=item["gender"],
            date_dob=date_dob,
            date_granularity_dob=date_granularity_dob,
            date_dod=date_dod,
            date_granularity_dod=date_granularity_dod,
        )
        if not had_alias_result:
            # Only set the name and ID values on non-alias results, otherwise
            # you overwrite the good name with the alias name.
            person.cl_id = item["cl_id"]
            person.name_first = item["firstname"]
            person.name_middle = item["midname"]
            person.name_last = item["lastname"]
            person.name_suffix = get_suffix(item["suffname"])

        if not testing:
            person.save()

        if not pd.isnull(item["nickname"]):
            person_alias = Person(
                cl_id=item["cl_id"] + "-alias-1",
                name_first=item["nickname"],
                name_middle=item["midname"],
                name_last=item["lastname"],
                name_suffix=get_suffix(item["suffname"]),
                is_alias_of=person,
            )
            if not testing:
                person_alias.save()

    if "colr" in item["cl_id"]:
        courtid = get_state_court_object(
            item["court"] + " of " + item["state"]
        )
    else:
        courtid = get_state_court_object(item["court"])

    if courtid is None:
        print(item)
        raise Exception

    # assign start date
    date_start, date_granularity_start = process_date(
        item["startyear"], item["startmonth"], item["startday"]
    )

    if item["endyear"] > 2016:
        item["endyear"] = None
    date_termination, date_granularity_termination = process_date(
        item["endyear"], item["endmonth"], item["endday"]
    )

    judgeship = Position(
        person=person,
        court_id=courtid,
        position_type="jud",
        date_start=date_start,
        date_granularity_start=date_granularity_start,
        date_termination=date_termination,
        date_granularity_termination=date_granularity_termination,
        # how_selected = get_select(courtid,item['startyear']),
        termination_reason=item["howended"],
    )

    if not testing:
        judgeship.save()

    if not pd.isnull(item["college"]):
        if ";" in item["college"]:
            colls = [x.strip() for x in item["college"].split(";")]
        else:
            colls = [item["college"].strip()]
        for coll in colls:
            school = get_school(coll)
            if school is not None:
                college = Education(
                    person=person, school=school, degree_level="ba"
                )
                if not testing:
                    college.save()

    if not pd.isnull(item["lawschool"]):
        if ";" in item["lawschool"]:
            lschools = [x.strip() for x in item["lawschool"].split(";")]
        else:
            lschools = [item["lawschool"].strip()]

        for L in lschools:
            lschool = get_school(L)
            if lschool is not None:
                lawschool = Education(
                    person=person, school=lschool, degree_level="jd"
                )
                if not testing:
                    lawschool.save()

    # iterate through job variables and add to career if applicable
    for jobvar in [
        "prevjudge",
        "prevprivate",
        "prevpolitician",
        "prevprof",
        "postjudge",
        "postprivate",
        "postpolitician",
        "postprof",
    ]:
        if pd.isnull(item[jobvar]) or item[jobvar] == 0:
            continue
        position_type = None
        if "judge" in jobvar:
            position_type = "jud"
        elif "private" in jobvar:
            position_type = "prac"
        elif "politician" in jobvar:
            position_type = "legis"
        elif "prof" in jobvar:
            position_type = "prof"

        job_start = None
        job_end = None
        if "prev" in jobvar:
            job_start = date_start.year - 1
            job_end = date_start.year - 1
        if "post" in jobvar:
            if date_termination is None:
                continue
            job_start = date_termination.year + 1
            job_end = date_termination.year + 1

        job = Position(
            person=person,
            position_type=position_type,
            date_start=date(job_start, 1, 1),
            date_granularity_start="%Y",
            date_termination=date(job_end, 1, 1),
            date_granularity_termination="%Y",
        )
        if not testing:
            job.save()

    if not pd.isnull(item["politics"]):
        politics = PoliticalAffiliation(
            person=person, political_party=item["politics"].lower()
        )
        if not testing:
            politics.save()

    if not pd.isnull(item["links"]):
        links = item["links"]
        if ";" in links:
            urls = [x.strip() for x in links.split(";")]
        else:
            urls = [links]
        for url in urls:
            source = Source(person=person, notes=item["notes"], url=url)

            if not testing:
                source.save()
