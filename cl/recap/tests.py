import json
import os
from datetime import date
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from juriscraper.pacer import PacerRssFeed
from rest_framework.status import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_400_BAD_REQUEST,
    HTTP_401_UNAUTHORIZED,
)
from rest_framework.test import APIClient

from cl.lib.storage import clobbering_get_name
from cl.people_db.models import (
    Attorney,
    AttorneyOrganizationAssociation,
    CriminalComplaint,
    CriminalCount,
    Party,
    PartyType,
    Role,
)
from cl.recap.management.commands.import_idb import Command
from cl.recap.mergers import (
    add_attorney,
    add_docket_entries,
    add_parties_and_attorneys,
    find_docket_object,
    normalize_long_description,
    update_case_names,
    update_docket_metadata,
)
from cl.recap.models import (
    PROCESSING_STATUS,
    REQUEST_TYPE,
    UPLOAD_TYPE,
    EmailProcessingQueue,
    PacerFetchQueue,
    ProcessingQueue,
)
from cl.recap.tasks import (
    do_pacer_fetch,
    fetch_pacer_doc_by_rd,
    process_recap_appellate_docket,
    process_recap_attachment,
    process_recap_claims_register,
    process_recap_docket,
    process_recap_pdf,
    process_recap_zip,
)
from cl.search.models import (
    Docket,
    DocketEntry,
    OriginatingCourtInformation,
    RECAPDocument,
)
from cl.tests import fakes


@mock.patch("cl.recap.views.process_recap_upload")
class RecapUploadsTest(TestCase):
    """Test the rest endpoint, but exclude the processing tasks."""

    fixtures = ["canb_court.json"]

    def setUp(self) -> None:
        self.client = APIClient()
        self.user = User.objects.get(username="recap")
        token = "Token " + self.user.auth_token.key
        self.client.credentials(HTTP_AUTHORIZATION=token)
        self.path = reverse("processingqueue-list", kwargs={"version": "v3"})
        f = SimpleUploadedFile("file.txt", b"file content more content")
        self.data = {
            "court": "akd",
            "pacer_case_id": "asdf",
            "pacer_doc_id": 24,
            "document_number": 1,
            "filepath_local": f,
            "upload_type": UPLOAD_TYPE.PDF,
        }

    def test_uploading_a_pdf(self, mock):
        """Can we upload a document and have it be saved correctly?"""
        r = self.client.post(self.path, self.data)
        self.assertEqual(r.status_code, HTTP_201_CREATED)

        j = json.loads(r.content)
        self.assertEqual(j["court"], "akd")
        self.assertEqual(j["document_number"], 1)
        self.assertEqual(j["pacer_case_id"], "asdf")
        mock.assert_called()

    def test_uploading_a_zip(self, mock):
        """Can we upload a zip?"""
        self.data.update({"upload_type": UPLOAD_TYPE.DOCUMENT_ZIP})
        del self.data["pacer_doc_id"]
        r = self.client.post(self.path, self.data)
        self.assertEqual(r.status_code, HTTP_201_CREATED)
        mock.assert_called()

    def test_uploading_a_docket(self, mock):
        """Can we upload a docket and have it be saved correctly?

        Note that this works fine even though we're not actually uploading a
        docket due to the mock.
        """
        self.data.update(
            {"upload_type": UPLOAD_TYPE.DOCKET, "document_number": ""}
        )
        del self.data["pacer_doc_id"]
        r = self.client.post(self.path, self.data)
        self.assertEqual(r.status_code, HTTP_201_CREATED)

        j = json.loads(r.content)
        path = reverse(
            "processingqueue-detail", kwargs={"version": "v3", "pk": j["id"]}
        )
        r = self.client.get(path)
        self.assertEqual(r.status_code, HTTP_200_OK)

    def test_uploading_a_claims_registry_page(self, mock):
        """Can we upload claims registry data?"""
        self.data.update(
            {
                "upload_type": UPLOAD_TYPE.CLAIMS_REGISTER,
                "document_number": "",
                "pacer_doc_id": "",
                "court": "canb",
            }
        )
        r = self.client.post(self.path, self.data)
        self.assertEqual(r.status_code, HTTP_201_CREATED)
        mock.assert_called()

    def test_uploading_an_attachment_page(self, mock):
        """Can we upload an attachment page and have it be saved correctly?"""
        self.data.update(
            {
                "upload_type": UPLOAD_TYPE.ATTACHMENT_PAGE,
                "document_number": "",
            }
        )
        r = self.client.post(self.path, self.data)
        self.assertEqual(r.status_code, HTTP_201_CREATED)

        j = json.loads(r.content)
        path = reverse(
            "processingqueue-detail", kwargs={"version": "v3", "pk": j["id"]}
        )
        r = self.client.get(path)
        self.assertEqual(r.status_code, HTTP_200_OK)

    def test_numbers_in_docket_uploads_fail(self, mock):
        """Are invalid uploads denied?

        For example, if you're uploading a Docket, you shouldn't be providing a
        document number.
        """
        self.data["upload_type"] = UPLOAD_TYPE.DOCKET
        r = self.client.post(self.path, self.data)
        self.assertEqual(r.status_code, HTTP_400_BAD_REQUEST)

    def test_district_court_in_appellate_upload_fails(self, mock):
        """If you send a district court to an appellate endpoint, does it
        fail?
        """
        self.data.update({"upload_type": UPLOAD_TYPE.APPELLATE_DOCKET})
        del self.data["pacer_doc_id"]
        del self.data["document_number"]
        r = self.client.post(self.path, self.data)
        self.assertEqual(r.status_code, HTTP_400_BAD_REQUEST)

    def test_appellate_court_in_district_upload_fails(self, mock):
        """If you send appellate court info to a distric court, does it
        fail?
        """
        self.data.update(
            {"upload_type": UPLOAD_TYPE.DOCKET, "court": "scotus"}
        )
        del self.data["pacer_doc_id"]
        del self.data["document_number"]
        r = self.client.post(self.path, self.data)
        self.assertEqual(r.status_code, HTTP_400_BAD_REQUEST)

    def test_string_for_document_number_fails(self, mock):
        self.data["document_number"] = "asdf"  # Not an int.
        r = self.client.post(self.path, self.data)
        self.assertEqual(r.status_code, HTTP_400_BAD_REQUEST)

    def test_no_numbers_in_docket_uploads_work(self, mock):
        self.data["upload_type"] = UPLOAD_TYPE.DOCKET
        del self.data["pacer_doc_id"]
        del self.data["document_number"]
        r = self.client.post(self.path, self.data)
        self.assertEqual(r.status_code, HTTP_201_CREATED)

    def test_pdf_without_pacer_case_id_works(self, mock):
        """Do we allow PDFs lacking a pacer_case_id value?"""
        del self.data["pacer_case_id"]
        r = self.client.post(self.path, self.data)
        self.assertEqual(r.status_code, HTTP_201_CREATED)

    def test_uploading_non_ascii(self, mock):
        """Can we handle it if a client sends non-ascii strings?"""
        self.data["pacer_case_id"] = u"☠☠☠"
        r = self.client.post(self.path, self.data)
        self.assertEqual(r.status_code, HTTP_201_CREATED)
        mock.assert_called()

    def test_disallowed_court(self, mock):
        """Do posts fail if a bad court is given?"""
        self.data["court"] = "ala"
        r = self.client.post(self.path, self.data)
        self.assertEqual(r.status_code, HTTP_400_BAD_REQUEST)

    def test_fails_no_document(self, mock):
        """Do posts fail if the lack an attachment?"""
        del self.data["filepath_local"]
        r = self.client.post(self.path, self.data)
        self.assertEqual(r.status_code, HTTP_400_BAD_REQUEST)

    def test_user_associated_properly(self, mock):
        """Does the user get associated after the upload?"""
        r = self.client.post(self.path, self.data)
        j = json.loads(r.content)
        processing_request = ProcessingQueue.objects.get(pk=j["id"])
        self.assertEqual(self.user.pk, processing_request.uploader_id)
        mock.assert_called()

    def test_ensure_no_users_in_response(self, mock):
        """Is all user information excluded from the processing queue?"""
        r = self.client.post(self.path, self.data)
        j = json.loads(r.content)
        for bad_key in ["uploader", "user"]:
            with self.assertRaises(KeyError):
                # noinspection PyStatementEffect
                j[bad_key]
        mock.assert_called()


@mock.patch("cl.recap.tasks.DocketReport", new=fakes.FakeDocketReport)
@mock.patch(
    "cl.recap.tasks.PossibleCaseNumberApi",
    new=fakes.FakePossibleCaseNumberApi,
)
class RecapDocketFetchApiTest(TestCase):
    """Tests for the RECAP docket Fetch API

    The general approach here is to use mocks to separate out the serialization
    and API tests from the processing logic tests.
    """

    fixtures = ["judge_judy.json", "test_objects_search.json"]

    COURT = "scotus"

    def setUp(self) -> None:
        self.user = User.objects.get(username="recap")

    def test_fetch_docket_by_docket_number(self) -> None:
        """Can we do a simple fetch of a docket from PACER?"""
        fq = PacerFetchQueue.objects.create(
            user=self.user,
            request_type=REQUEST_TYPE.DOCKET,
            court_id=self.COURT,
            docket_number=fakes.DOCKET_NUMBER,
        )
        result = do_pacer_fetch(fq)
        # Wait for the chain to complete
        result.get()

        fq.refresh_from_db()
        self.assertEqual(fq.status, PROCESSING_STATUS.SUCCESSFUL)

    def test_fetch_docket_by_pacer_case_id(self) -> None:
        fq = PacerFetchQueue.objects.create(
            user=self.user,
            request_type=REQUEST_TYPE.DOCKET,
            court_id=self.COURT,
            pacer_case_id="104490",
        )
        result = do_pacer_fetch(fq)
        result.get()
        fq.refresh_from_db()
        self.assertEqual(fq.status, PROCESSING_STATUS.SUCCESSFUL)

    def test_fetch_docket_by_docket_id(self) -> None:
        fq = PacerFetchQueue.objects.create(
            user=self.user, request_type=REQUEST_TYPE.DOCKET, docket_id=1
        )
        result = do_pacer_fetch(fq)
        result.get()
        fq.refresh_from_db()
        self.assertEqual(fq.status, PROCESSING_STATUS.SUCCESSFUL)


@mock.patch(
    "cl.corpus_importer.tasks.FreeOpinionReport",
    new=fakes.FakeFreeOpinionReport,
)
@mock.patch(
    "cl.recap.tasks.get_pacer_cookie_from_cache",
    return_value={"cookie": "foo"},
)
class RecapPdfFetchApiTest(TestCase):
    """Can we fetch PDFs properly?"""

    fixtures = ["recap_docs.json"]

    def setUp(self) -> None:
        self.fq = PacerFetchQueue.objects.create(
            user=User.objects.get(username="recap"),
            request_type=REQUEST_TYPE.PDF,
            recap_document_id=1,
        )
        self.rd = self.fq.recap_document

    def tearDown(self) -> None:
        RECAPDocument.objects.update(is_available=True)
        self.rd.refresh_from_db()

    @mock.patch(
        "cl.lib.storage.get_name_by_incrementing",
        side_effect=clobbering_get_name,
    )
    def test_fetch_unavailable_pdf(self, mock_get_cookie, mock_get_name):
        """Can we do a simple fetch of a PDF from PACER?"""
        self.rd.is_available = False
        self.rd.save()

        self.assertFalse(self.rd.is_available)
        result = do_pacer_fetch(self.fq)
        result.get()
        self.fq.refresh_from_db()
        self.rd.refresh_from_db()
        self.assertEqual(self.fq.status, PROCESSING_STATUS.SUCCESSFUL)
        self.assertTrue(self.rd.is_available)

    def test_fetch_available_pdf(self, mock_get_cookie):
        orig_date_modified = self.rd.date_modified

        response = fetch_pacer_doc_by_rd(self.rd.pk, self.fq.pk)
        self.assertIsNone(
            response,
            "Did not get None from fetch, indicating that the fetch did "
            "something, but it shouldn't have done anything when the doc is "
            "available.",
        )

        self.fq.refresh_from_db()
        self.rd.refresh_from_db()
        self.assertEqual(self.fq.status, PROCESSING_STATUS.SUCCESSFUL)
        self.assertEqual(
            orig_date_modified,
            self.rd.date_modified,
            msg="rd updated even though it was available.",
        )


class RecapAttPageFetchApiTest(TestCase):
    fixtures = ["recap_docs.json"]

    def setUp(self) -> None:
        self.fq = PacerFetchQueue.objects.create(
            user=User.objects.get(username="recap"),
            request_type=REQUEST_TYPE.ATTACHMENT_PAGE,
            recap_document_id=1,
        )
        self.rd = self.fq.recap_document
        self.rd.pacer_doc_id = "17711118263"
        self.rd.save()

    def test_fetch_attachment_page_no_pacer_doc_id(self) -> None:
        """Can we do a simple fetch of an attachment page from PACER?"""
        self.rd.pacer_doc_id = ""
        self.rd.save()

        result = do_pacer_fetch(self.fq)
        result.get()

        self.fq.refresh_from_db()
        self.assertEqual(self.fq.status, PROCESSING_STATUS.NEEDS_INFO)

    def test_fetch_att_page_no_cookies(self) -> None:
        result = do_pacer_fetch(self.fq)
        result.get()

        self.fq.refresh_from_db()
        self.assertEqual(self.fq.status, PROCESSING_STATUS.FAILED)
        self.assertIn("Unable to find cached cookies", self.fq.message)

    @mock.patch(
        "cl.recap.tasks.get_pacer_cookie_from_cache",
        return_value={"pacer_cookie": "foo"},
    )
    @mock.patch(
        "cl.corpus_importer.tasks.AttachmentPage",
        new=fakes.FakeAttachmentPage,
    )
    @mock.patch(
        "cl.recap.mergers.AttachmentPage", new=fakes.FakeAttachmentPage
    )
    def test_fetch_att_page_all_systems_go(self, mock_get_cookies):
        result = do_pacer_fetch(self.fq)
        result.get()

        self.fq.refresh_from_db()
        self.assertEqual(self.fq.status, PROCESSING_STATUS.SUCCESSFUL)
        self.assertIn("Successfully completed fetch", self.fq.message)


class ProcessingQueueApiFilterTest(TestCase):
    def setUp(self) -> None:
        self.client = APIClient()
        self.user = User.objects.get(username="recap")
        token = "Token " + self.user.auth_token.key
        self.client.credentials(HTTP_AUTHORIZATION=token)
        self.path = reverse("processingqueue-list", kwargs={"version": "v3"})
        # Set up for making PQ objects.
        filename = "file.pdf"
        file_content = b"file content more content"
        f = SimpleUploadedFile(filename, file_content)
        self.params = {
            "court_id": "scotus",
            "uploader": self.user,
            "pacer_case_id": "asdf",
            "pacer_doc_id": "asdf",
            "document_number": "1",
            "filepath_local": f,
            "status": PROCESSING_STATUS.ENQUEUED,
            "upload_type": UPLOAD_TYPE.PDF,
        }

    def test_pq_filters(self) -> None:
        """Can we filter with the status and upload_type filters?"""
        # Create two PQ objects with different values.
        ProcessingQueue.objects.create(**self.params)
        self.params["status"] = PROCESSING_STATUS.FAILED
        self.params["upload_type"] = UPLOAD_TYPE.ATTACHMENT_PAGE
        ProcessingQueue.objects.create(**self.params)

        # Then try filtering.
        total_number_results = 2
        r = self.client.get(self.path)
        j = json.loads(r.content)
        self.assertEqual(j["count"], total_number_results)

        total_awaiting_processing = 1
        r = self.client.get(self.path, {"status": PROCESSING_STATUS.ENQUEUED})
        j = json.loads(r.content)
        self.assertEqual(j["count"], total_awaiting_processing)

        total_pdfs = 1
        r = self.client.get(self.path, {"upload_type": UPLOAD_TYPE.PDF})
        j = json.loads(r.content)
        self.assertEqual(j["count"], total_pdfs)


class RecapEmailToEmailProcessingQueueTest(TestCase):
    """Test the rest endpoint, but exclude the processing tasks."""

    fixtures = ["canb_court.json"]

    def setUp(self) -> None:
        self.client = APIClient()
        self.user = User.objects.get(username="recap-email")
        token = "Token " + self.user.auth_token.key
        self.client.credentials(HTTP_AUTHORIZATION=token)
        self.path = "/api/rest/v3/recap-email/"
        test_dir = os.path.join(
            settings.INSTALL_ROOT, "cl", "recap", "test_assets"
        )
        with open(os.path.join(test_dir, "recap_mail_receipt.json")) as file:
            recap_mail_receipt = json.loads(file.read())
            self.data = {
                "court": "akd",
                "mail": recap_mail_receipt["mail"],
                "receipt": recap_mail_receipt["receipt"],
            }

    def test_non_pacer_court_fails(self):
        self.data["court"] = "scotus"
        r = self.client.post(self.path, self.data, format="json")
        j = json.loads(r.content)
        self.assertEqual(r.status_code, HTTP_400_BAD_REQUEST)
        self.assertEqual(
            j["non_field_errors"], ["scotus is not a PACER court ID."]
        )

    def test_missing_mail_properties_fails(self):
        del self.data["mail"]["headers"]
        r = self.client.post(self.path, self.data, format="json")
        j = json.loads(r.content)
        self.assertEqual(r.status_code, HTTP_400_BAD_REQUEST)
        self.assertEqual(
            j["non_field_errors"],
            ["The JSON value at key 'mail' should include 'headers'."],
        )

    def test_missing_receipt_properties_fails(self):
        del self.data["receipt"]["recipients"]
        r = self.client.post(self.path, self.data, format="json")
        j = json.loads(r.content)
        self.assertEqual(r.status_code, HTTP_400_BAD_REQUEST)
        self.assertEqual(
            j["non_field_errors"],
            ["The JSON value at key 'receipt' should include 'recipients'."],
        )

    def test_email_processing_queue_create(self):
        self.assertEqual(EmailProcessingQueue.objects.count(), 0)
        self.client.post(self.path, self.data, format="json")
        self.assertEqual(EmailProcessingQueue.objects.count(), 1)


class DebugRecapUploadtest(TestCase):
    """Test uploads with debug set to True. Do these uploads avoid causing
    problems?
    """

    def setUp(self) -> None:
        self.user = User.objects.get(username="recap")
        self.pdf = SimpleUploadedFile("file.pdf", b"file content more content")
        test_dir = os.path.join(
            settings.INSTALL_ROOT, "cl", "recap", "test_assets"
        )
        self.d_filename = "cand.html"
        d_path = os.path.join(test_dir, self.d_filename)
        with open(d_path, "rb") as f:
            self.docket = SimpleUploadedFile(self.d_filename, f.read())

        self.att_filename = "dcd_04505578698.html"
        att_path = os.path.join(test_dir, self.att_filename)
        with open(att_path, "rb") as f:
            self.att = SimpleUploadedFile(self.att_filename, f.read())

    def tearDown(self) -> None:
        ProcessingQueue.objects.all().delete()
        Docket.objects.all().delete()
        DocketEntry.objects.all().delete()
        RECAPDocument.objects.all().delete()

    @mock.patch("cl.recap.tasks.extract_recap_pdf")
    @mock.patch(
        "cl.lib.storage.get_name_by_incrementing",
        side_effect=clobbering_get_name,
    )
    def test_debug_does_not_create_rd(self, mock_extract, mock_get_name):
        """If debug is passed, do we avoid creating recap documents?"""
        docket = Docket.objects.create(
            source=0, court_id="scotus", pacer_case_id="asdf"
        )
        DocketEntry.objects.create(docket=docket, entry_number=1)
        pq = ProcessingQueue.objects.create(
            court_id="scotus",
            uploader=self.user,
            pacer_case_id="asdf",
            pacer_doc_id="asdf",
            document_number="1",
            filepath_local=self.pdf,
            upload_type=UPLOAD_TYPE.PDF,
            debug=True,
        )
        process_recap_pdf(pq.pk)
        self.assertEqual(RECAPDocument.objects.count(), 0)
        mock_extract.assert_not_called()

    @mock.patch("cl.recap.mergers.add_attorney")
    def test_debug_does_not_create_docket(self, add_atty_mock):
        """If debug is passed, do we avoid creating a docket?"""
        pq = ProcessingQueue.objects.create(
            court_id="scotus",
            uploader=self.user,
            pacer_case_id="asdf",
            filepath_local=self.docket,
            upload_type=UPLOAD_TYPE.DOCKET,
            debug=True,
        )
        process_recap_docket(pq.pk)
        self.assertEqual(Docket.objects.count(), 0)
        self.assertEqual(DocketEntry.objects.count(), 0)
        self.assertEqual(RECAPDocument.objects.count(), 0)

    @mock.patch("cl.recap.tasks.add_items_to_solr")
    def test_debug_does_not_create_recap_documents(self, mock):
        """If debug is passed, do we avoid creating recap documents?"""
        d = Docket.objects.create(
            source=0, court_id="scotus", pacer_case_id="asdf"
        )
        de = DocketEntry.objects.create(docket=d, entry_number=1)
        RECAPDocument.objects.create(
            docket_entry=de,
            document_number="1",
            pacer_doc_id="04505578698",
            document_type=RECAPDocument.PACER_DOCUMENT,
        )
        pq = ProcessingQueue.objects.create(
            court_id="scotus",
            uploader=self.user,
            upload_type=UPLOAD_TYPE.ATTACHMENT_PAGE,
            filepath_local=self.att,
            debug=True,
        )
        process_recap_attachment(pq.pk)
        self.assertEqual(Docket.objects.count(), 1)
        self.assertEqual(DocketEntry.objects.count(), 1)
        self.assertEqual(RECAPDocument.objects.count(), 1)
        mock.assert_not_called()


class RecapPdfTaskTest(TestCase):
    def setUp(self) -> None:
        user = User.objects.get(username="recap")
        self.filename = "file.pdf"
        self.file_content = b"file content more content"
        f = SimpleUploadedFile(self.filename, self.file_content)
        sha1 = "dcfdea519bef494e9672b94a4a03a49d591e3762"  # <-- SHA1 for above
        with mock.patch(
            "cl.lib.storage.get_name_by_incrementing",
            side_effect=clobbering_get_name,
        ):
            self.pq = ProcessingQueue.objects.create(
                court_id="scotus",
                uploader=user,
                pacer_case_id="asdf",
                pacer_doc_id="asdf",
                document_number="1",
                filepath_local=f,
                upload_type=UPLOAD_TYPE.PDF,
            )
        self.docket = Docket.objects.create(
            source=Docket.DEFAULT, court_id="scotus", pacer_case_id="asdf"
        )
        self.de = DocketEntry.objects.create(
            docket=self.docket, entry_number=1
        )
        self.rd = RECAPDocument.objects.create(
            docket_entry=self.de,
            document_type=RECAPDocument.PACER_DOCUMENT,
            document_number=1,
            pacer_doc_id="asdf",
            sha1=sha1,
        )

    def tearDown(self) -> None:
        self.pq.filepath_local.delete()
        self.pq.delete()
        try:
            self.docket.delete()  # This cascades to self.de and self.rd
        except (Docket.DoesNotExist, AssertionError):
            pass

    def test_pq_has_default_status(self) -> None:
        self.assertTrue(self.pq.status == PROCESSING_STATUS.ENQUEUED)

    @mock.patch("cl.recap.tasks.extract_recap_pdf")
    @mock.patch(
        "cl.lib.storage.get_name_by_incrementing",
        side_effect=clobbering_get_name,
    )
    def test_recap_document_already_exists(self, mock_get_name, mock_extract):
        """We already have everything"""
        # Update self.rd so it looks like it is already all good.
        self.rd.is_available = True
        cf = ContentFile(self.file_content)
        self.rd.filepath_local.save(self.filename, cf)

        rd = process_recap_pdf(self.pq.pk)

        # Did we avoid creating new objects?
        self.assertEqual(rd, self.rd)
        self.assertEqual(rd.docket_entry, self.de)
        self.assertEqual(rd.docket_entry.docket, self.docket)

        # Did we update pq appropriately?
        self.pq.refresh_from_db()
        self.assertEqual(self.pq.status, PROCESSING_STATUS.SUCCESSFUL)
        self.assertEqual(
            self.pq.error_message, "Successful upload! Nice work."
        )
        self.assertFalse(self.pq.filepath_local)
        self.assertEqual(self.pq.docket_id, self.docket.pk)
        self.assertEqual(self.pq.docket_entry_id, self.de.pk)
        self.assertEqual(self.pq.recap_document_id, self.rd.pk)

        # Did we correctly avoid running document extraction?
        mock_extract.assert_not_called()
        mock_get_name.assert_called()

    def test_only_the_docket_already_exists(self) -> None:
        """Never seen this docket entry before?

        Alas, we fail. In theory, this shouldn't happen.
        """
        self.de.delete()
        with self.assertRaises(DocketEntry.DoesNotExist):
            process_recap_pdf(self.pq.pk)
        self.pq.refresh_from_db()
        # This doesn't do the celery retries, unfortunately. If we get that
        # working, the correct status is PROCESSING_STATUS.FAILED.
        self.assertEqual(self.pq.status, PROCESSING_STATUS.QUEUED_FOR_RETRY)
        self.assertIn("Unable to find docket entry", self.pq.error_message)

    @mock.patch("cl.recap.tasks.extract_recap_pdf")
    @mock.patch(
        "cl.lib.storage.get_name_by_incrementing",
        side_effect=clobbering_get_name,
    )
    def test_docket_and_docket_entry_already_exist(
        self, mock_get_name, mock_extract
    ):
        """What happens if we have everything but the PDF?

        This is the good case. We simply create a new item.
        """
        self.rd.delete()
        rd = process_recap_pdf(self.pq.pk)
        self.assertTrue(rd.is_available)
        self.assertTrue(rd.sha1)
        self.assertTrue(rd.filepath_local)
        self.assertIn("gov.uscourts.scotus.asdf.1.0", rd.filepath_local.name)

        mock_get_name.assert_called()
        mock_extract.assert_called_once()

        self.pq.refresh_from_db()
        self.assertEqual(self.pq.status, PROCESSING_STATUS.SUCCESSFUL)
        self.assertEqual(
            self.pq.error_message, "Successful upload! Nice work."
        )
        self.assertFalse(self.pq.filepath_local)

    def test_nothing_already_exists(self) -> None:
        """If a PDF is uploaded but there's no recap document and no docket do
        we fail?

        In practice, this shouldn't happen.
        """
        self.docket.delete()
        with self.assertRaises(Docket.DoesNotExist):
            process_recap_pdf(self.pq.pk)
        self.pq.refresh_from_db()
        # This doesn't do the celery retries, unfortunately. If we get that
        # working, the correct status is PROCESSING_STATUS.FAILED.
        self.assertEqual(self.pq.status, PROCESSING_STATUS.QUEUED_FOR_RETRY)
        self.assertIn("Unable to find docket", self.pq.error_message)


class RecapZipTaskTest(TestCase):
    """Do we do good things when people send us zips?"""

    test_dir = os.path.join(
        settings.INSTALL_ROOT, "cl", "recap", "test_assets"
    )

    def setUp(self) -> None:
        user = User.objects.get(username="recap")
        self.filename = "1-20-cv-10189-FDS.zip"
        self.file_path = os.path.join(self.test_dir, self.filename)
        with open(self.file_path, "rb") as f:
            self.file_content = f.read()
        f = SimpleUploadedFile(self.filename, self.file_content)
        self.pq = ProcessingQueue.objects.create(
            court_id="scotus",
            uploader=user,
            pacer_case_id="asdf",
            filepath_local=f,
            upload_type=UPLOAD_TYPE.DOCUMENT_ZIP,
        )
        self.docket = Docket.objects.create(
            source=Docket.DEFAULT, court_id="scotus", pacer_case_id="asdf"
        )
        self.de = DocketEntry.objects.create(
            docket=self.docket, entry_number=12
        )
        doc12_sha1 = "130c52020a3d3ce7f0dbd46361b242493abf8b43"
        self.doc12 = RECAPDocument.objects.create(
            docket_entry=self.de,
            document_type=RECAPDocument.PACER_DOCUMENT,
            document_number=12,
            pacer_doc_id="asdf",
            sha1=doc12_sha1,
        )
        doc12_att1_sha1 = "0ce3a2df4f429f94a8f579eac4d47ba42dd66eac"
        self.doc12_att1 = RECAPDocument.objects.create(
            docket_entry=self.de,
            document_type=RECAPDocument.ATTACHMENT,
            document_number=12,
            attachment_number=1,
            pacer_doc_id="asdf",
            sha1=doc12_att1_sha1,
        )
        self.docs = [self.doc12, self.doc12_att1]

    def tearDown(self) -> None:
        Docket.objects.all().delete()
        ProcessingQueue.objects.all().delete()

    @mock.patch("cl.recap.tasks.extract_recap_pdf")
    @mock.patch(
        "cl.lib.storage.get_name_by_incrementing",
        side_effect=clobbering_get_name,
    )
    def test_simple_zip_upload(self, mock_get_name, mock_extract):
        """Do we unpack the zip and process it's contents properly?"""
        # The original pq should be marked as complete with a good message.
        results = process_recap_zip(self.pq.pk)
        self.pq.refresh_from_db()
        self.assertEqual(self.pq.status, PROCESSING_STATUS.SUCCESSFUL)
        self.assertTrue(
            self.pq.error_message.startswith(
                "Successfully created ProcessingQueue objects: "
            ),
        )

        # A new pq should be created for each document
        expected_new_pq_count = 2
        actual_new_pq_count = len(results["new_pqs"])
        self.assertEqual(
            expected_new_pq_count,
            actual_new_pq_count,
            msg="Should have %s pq items in the DB, two from inside the zip, "
            "and one for the zip itself. Instead got %s."
            % (expected_new_pq_count, actual_new_pq_count),
        )

        # Wait for all the tasks to finish
        for task in results["tasks"]:
            task.wait(timeout=5, interval=0.25)

        # Are the PDF PQ's marked as successful?
        for new_pq in results["new_pqs"]:
            new_pq = ProcessingQueue.objects.get(pk=new_pq)
            self.assertEqual(new_pq.status, PROCESSING_STATUS.SUCCESSFUL)

        # Are the documents marked as available?
        for doc in self.docs:
            doc.refresh_from_db()
            self.assertTrue(
                doc.is_available,
                msg="Doc %s was not marked as available. This may mean that "
                "it was not processed properly by the PDF processor.",
            )

        # Was the mock called once per PDF in the zip?
        expected_call_count = len(results["new_pqs"])
        self.assertEqual(mock_extract.call_count, expected_call_count)
        mock_get_name.assert_called()


class RecapAddAttorneyTest(TestCase):
    def setUp(self) -> None:
        self.atty_org_name = "Lane Powell LLC"
        self.atty_phone = "907-276-2631"
        self.atty_email = "jamiesonb@lanepowell.com"
        self.atty_name = "Brewster H. Jamieson"
        self.atty = {
            "contact": "{org_name}\n"
            "301 W. Nothern Lights Blvd., Suite 301\n"
            "Anchorage, AK 99503-2648\n"
            "{phone}\n"
            "Fax: 907-276-2631\n"
            "Email: {email}\n".format(
                org_name=self.atty_org_name,
                phone=self.atty_phone,
                email=self.atty_email,
            ),
            "name": self.atty_name,
            "roles": [
                {"role": Role.ATTORNEY_LEAD, "date_action": None},
                {"role": Role.ATTORNEY_TO_BE_NOTICED, "date_action": None},
            ],
        }
        self.d = Docket.objects.create(
            source=0,
            court_id="scotus",
            pacer_case_id="asdf",
            date_filed=date(2017, 1, 1),
        )
        self.p = Party.objects.create(name="John Wesley Powell")

    def test_new_atty_to_db(self) -> None:
        """Can we add a new atty to the DB when none exist?"""
        a_pk = add_attorney(self.atty, self.p, self.d)
        a = Attorney.objects.get(pk=a_pk)
        self.assertEqual(a.contact_raw, self.atty["contact"])
        self.assertEqual(a.name, self.atty["name"])
        self.assertTrue(
            AttorneyOrganizationAssociation.objects.filter(
                attorney=a,
                attorney_organization__name=self.atty_org_name,
                docket=self.d,
            ).exists(),
            msg="Unable to find attorney organization association.",
        )
        self.assertEqual(a.email, self.atty_email)
        self.assertEqual(a.roles.all().count(), 2)

    def test_no_contact_info(self) -> None:
        """Do things work properly when we lack contact information?"""
        self.atty["contact"] = ""
        a_pk = add_attorney(self.atty, self.p, self.d)
        a = Attorney.objects.get(pk=a_pk)
        # No org info added because none provided:
        self.assertEqual(a.organizations.all().count(), 0)
        # But roles still get done.
        self.assertEqual(a.roles.all().count(), 2)

    def test_no_contact_info_another_already_exists(self) -> None:
        """If we lack contact info, and such a atty already exists (without
        contact info), do we properly consider them different people?
        """
        new_a = Attorney.objects.create(name=self.atty_name)
        self.atty["contact"] = ""
        a_pk = add_attorney(self.atty, self.p, self.d)
        a = Attorney.objects.get(pk=a_pk)
        self.assertNotEqual(a.pk, new_a.pk)

    def test_existing_roles_get_overwritten(self) -> None:
        """Do existing roles get overwritten with latest data?"""
        new_a = Attorney.objects.create(
            name=self.atty_name, email=self.atty_email
        )
        r = Role.objects.create(
            attorney=new_a, party=self.p, docket=self.d, role=Role.DISBARRED
        )
        a_pk = add_attorney(self.atty, self.p, self.d)
        a = Attorney.objects.get(pk=a_pk)
        self.assertEqual(new_a.pk, a.pk)
        roles = a.roles.all()
        self.assertEqual(roles.count(), 2)
        self.assertNotIn(r, roles)


class DocketCaseNameUpdateTest(TestCase):
    """Do we properly handle the nine cases of incoming case name
    information?
    """

    def setUp(self) -> None:
        self.d = Docket()
        self.v_case_name = "x v. y"
        self.new_case_name = "x v. z"
        self.uct = "Unknown Case Title"

    def test_new_v_old_v_updates(self) -> None:
        """Do we update if new is different and old has a value?"""
        self.d.case_name = self.v_case_name
        d = update_case_names(self.d, self.new_case_name)
        self.assertEqual(d.case_name, self.new_case_name)

    def test_new_v_old_uct_updates(self) -> None:
        """Do we update if new has a value and old is UCT"""
        self.d.case_name = self.uct
        d = update_case_names(self.d, self.new_case_name)
        self.assertEqual(d.case_name, self.new_case_name)

    def test_new_v_old_blank_updates(self) -> None:
        self.d.case_name = ""
        d = update_case_names(self.d, self.new_case_name)
        self.assertEqual(d.case_name, self.new_case_name)

    def test_new_uct_old_v_no_update(self) -> None:
        self.d.case_name = self.v_case_name
        d = update_case_names(self.d, self.uct)
        self.assertEqual(d.case_name, self.v_case_name)

    def test_new_uct_old_uct_no_update(self) -> None:
        self.d.case_name = self.uct
        d = update_case_names(self.d, self.uct)
        self.assertEqual(d.case_name, self.uct)

    def test_new_uct_old_blank_updates(self) -> None:
        self.d.case_name = ""
        d = update_case_names(self.d, self.uct)
        self.assertEqual(d.case_name, self.uct)

    def test_new_blank_old_v_no_update(self) -> None:
        self.d.case_name = self.v_case_name
        d = update_case_names(self.d, "")
        self.assertEqual(d.case_name, self.v_case_name)

    def test_new_blank_old_uct_no_update(self) -> None:
        self.d.case_name = self.uct
        d = update_case_names(self.d, "")
        self.assertEqual(d.case_name, self.uct)

    def test_new_blank_old_blank_no_update(self) -> None:
        self.d.case_name = ""
        d = update_case_names(self.d, "")
        self.assertEqual(d.case_name, "")


class TerminatedEntitiesTest(TestCase):
    """Do we handle things properly when new and old data have terminated
    entities (attorneys & parties)?

    There are four possibilities we need to handle properly:

     1. The scraped data has terminated entities (easy: update all
        existing and delete anything that's not touched).
     2. The scraped data lacks terminated entities and the current
        data lacks them too (easy: update as above).
     3. The scraped data lacks terminated entities and the current
        data has them (hard: update whatever is in common, keep
        terminated entities, disassociate the rest).

    """

    def setUp(self) -> None:
        # Docket: self.d has...
        #   Party: self.p via PartyType, which has...
        #     Attorney self.a via Role, and...
        #     Attorney self.extraneous_a2 via Role.
        #   Party: self.extraneous_p via PartyType, which has...
        #     Attorney: self.extraneous_a via Role.

        self.d = Docket.objects.create(
            source=0,
            court_id="scotus",
            pacer_case_id="asdf",
            date_filed=date(2017, 1, 1),
        )

        # One valid party and attorney.
        self.p = Party.objects.create(name="John Wesley Powell")
        PartyType.objects.create(docket=self.d, party=self.p, name="defendant")
        self.a = Attorney.objects.create(name="Roosevelt")
        Role.objects.create(
            docket=self.d,
            party=self.p,
            attorney=self.a,
            role=Role.ATTORNEY_LEAD,
        )

        # These guys should get disassociated whenever the new data comes in.
        self.extraneous_p = Party.objects.create(name="US Gubment")
        PartyType.objects.create(
            docket=self.d, party=self.extraneous_p, name="special intervenor"
        )
        self.extraneous_a = Attorney.objects.create(name="Matthew Lesko")
        Role.objects.create(
            docket=self.d,
            party=self.extraneous_p,
            attorney=self.extraneous_a,
            role=Role.ATTORNEY_LEAD,
        )

        # Extraneous attorney on a valid party. Should always be disassociated.
        # Typo:
        self.extraneous_a2 = Attorney.objects.create(name="Mathew Lesko")
        Role.objects.create(
            docket=self.d,
            party=self.p,
            attorney=self.extraneous_a2,
            role=Role.ATTORNEY_TO_BE_NOTICED,
        )

        self.new_powell_data = {
            "extra_info": "",
            "name": "John Wesley Powell",
            "type": "defendant",
            "attorneys": [
                {
                    "contact": "",
                    "name": "Roosevelt",
                    "roles": ["LEAD ATTORNEY"],
                }
            ],
            "date_terminated": None,
        }
        self.new_mccarthy_data = {
            "extra_info": "",
            "name": "Joseph McCarthy",
            "type": "commie",
            "attorneys": [],
            "date_terminated": date(1957, 5, 2),  # Historically accurate
        }
        self.new_party_data = [self.new_powell_data, self.new_mccarthy_data]

    def test_new_has_terminated_entities(self) -> None:
        """Do we update all existing data when scraped data has terminated
        entities?
        """
        add_parties_and_attorneys(self.d, self.new_party_data)
        # Docket should have two parties, Powell and McCarthy. This
        # implies that extraneous_p has been removed.
        self.assertEqual(self.d.parties.count(), 2)

        # Powell has an attorney. The rest are extraneous or don't have attys.
        role_count = Role.objects.filter(docket=self.d).count()
        self.assertEqual(role_count, 1)

    def test_new_lacks_terminated_entities_old_lacks_too(self) -> None:
        """Do we update all existing data when there aren't terminated entities
        at play?
        """
        self.new_mccarthy_data["date_terminated"] = None
        add_parties_and_attorneys(self.d, self.new_party_data)

        # Docket should have two parties, Powell and McCarthy. This
        # implies that extraneous_p has been removed.
        self.assertEqual(self.d.parties.count(), 2)

        # Powell has an attorney. The rest are extraneous or don't have attys.
        role_count = Role.objects.filter(docket=self.d).count()
        self.assertEqual(role_count, 1)

    def test_new_lacks_terminated_entities_old_has_them(self) -> None:
        """Do we update things properly when old has terminated parties, but
        new lacks them?

        Do we disassociate extraneous parties that aren't in the new data and
        aren't terminated?
        """
        # Add terminated attorney that's not in the new data.
        term_a = Attorney.objects.create(name="Robert Mueller")
        Role.objects.create(
            docket=self.d,
            attorney=term_a,
            party=self.p,
            role=Role.TERMINATED,
            date_action=date(2018, 3, 16),
        )

        # Add a terminated party that's not in the new data.
        term_p = Party.objects.create(name="Zainab Ahmad")
        PartyType.objects.create(
            docket=self.d,
            party=term_p,
            name="plaintiff",
            date_terminated=date(2018, 11, 4),
        )

        # Remove termination data from the new.
        self.new_mccarthy_data["date_terminated"] = None

        add_parties_and_attorneys(self.d, self.new_party_data)

        # Docket should have three parties, Powell and McCarthy from the new
        # data, and Ahmad from the old. This implies that extraneous_p has been
        # removed and that terminated parties have not.
        self.assertEqual(self.d.parties.count(), 3)

        # Powell now has has two attorneys, Robert Mueller and self.a. The rest
        # are extraneous or don't have attys.
        role_count = Role.objects.filter(docket=self.d).count()
        self.assertEqual(role_count, 2)

    def test_no_parties(self) -> None:
        """Do we keep the old parties when the new case has none?"""
        count_before = self.d.parties.count()
        add_parties_and_attorneys(self.d, [])
        self.assertEqual(self.d.parties.count(), count_before)


class RecapMinuteEntriesTest(TestCase):
    """Can we ingest minute and numberless entries properly?"""

    @staticmethod
    def make_path(filename):
        return os.path.join(
            settings.INSTALL_ROOT, "cl", "recap", "test_assets", filename
        )

    def make_pq(self, filename="azd.html", upload_type=UPLOAD_TYPE.DOCKET):
        """Make a simple pq object for processing"""
        path = self.make_path(filename)
        with open(path, "rb") as f:
            f = SimpleUploadedFile(filename, f.read())
        return ProcessingQueue.objects.create(
            court_id="scotus",
            uploader=self.user,
            pacer_case_id="asdf",
            filepath_local=f,
            upload_type=upload_type,
        )

    def setUp(self) -> None:
        self.user = User.objects.get(username="recap")

    def tearDown(self) -> None:
        pqs = ProcessingQueue.objects.all()
        for pq in pqs:
            pq.filepath_local.delete()
            pq.delete()
        Docket.objects.all().delete()

    def test_all_entries_ingested_without_duplicates(self) -> None:
        """Are all of the docket entries ingested?"""
        expected_entry_count = 23

        pq = self.make_pq()
        returned_data = process_recap_docket(pq.pk)
        d1 = Docket.objects.get(pk=returned_data["docket_pk"])
        self.assertEqual(d1.docket_entries.count(), expected_entry_count)

        pq = self.make_pq()
        returned_data = process_recap_docket(pq.pk)
        d2 = Docket.objects.get(pk=returned_data["docket_pk"])
        self.assertEqual(d1.pk, d2.pk)
        self.assertEqual(d2.docket_entries.count(), expected_entry_count)

    def test_multiple_numberless_entries_multiple_times(self) -> None:
        """Do we get the right number of entries when we add multiple
        numberless entries multiple times?
        """
        expected_entry_count = 25
        pq = self.make_pq("azd_multiple_unnumbered.html")
        returned_data = process_recap_docket(pq.pk)
        d1 = Docket.objects.get(pk=returned_data["docket_pk"])
        self.assertEqual(d1.docket_entries.count(), expected_entry_count)

        pq = self.make_pq("azd_multiple_unnumbered.html")
        returned_data = process_recap_docket(pq.pk)
        d2 = Docket.objects.get(pk=returned_data["docket_pk"])
        self.assertEqual(d1.pk, d2.pk)
        self.assertEqual(d2.docket_entries.count(), expected_entry_count)

    def test_appellate_cases_ok(self) -> None:
        """Do appellate cases get ordered/handled properly?"""
        expected_entry_count = 16
        pq = self.make_pq("ca1.html", upload_type=UPLOAD_TYPE.APPELLATE_DOCKET)
        returned_data = process_recap_appellate_docket(pq.pk)
        d1 = Docket.objects.get(pk=returned_data["docket_pk"])
        self.assertEqual(d1.docket_entries.count(), expected_entry_count)

    def test_rss_feed_ingestion(self) -> None:
        """Can we ingest RSS feeds without creating duplicates?"""
        court_id = "scotus"
        rss_feed = PacerRssFeed(court_id)
        rss_feed.is_bankruptcy = True  # Needed because we say SCOTUS above.
        with open(self.make_path("rss_sample_unnumbered_mdb.xml"), "rb") as f:
            text = f.read().decode()
        rss_feed._parse_text(text)
        docket = rss_feed.data[0]
        d = find_docket_object(
            court_id, docket["pacer_case_id"], docket["docket_number"]
        )
        update_docket_metadata(d, docket)
        d.save()

        expected_count = 1
        add_docket_entries(d, docket["docket_entries"])
        self.assertEqual(d.docket_entries.count(), expected_count)
        add_docket_entries(d, docket["docket_entries"])
        self.assertEqual(d.docket_entries.count(), expected_count)

    def test_dhr_merges_separate_docket_entries(self) -> None:
        """Does the docket history report merge separate minute entries if
        one entry has a short description, and the other has a long
        description?
        """
        # Create two unnumbered docket entries, one with a short description
        # and one with a long description. Then see what happens when you try
        # to add a DHR result (it should merge them).
        short_desc = "Entry one short description"
        long_desc = "Entry one long desc"
        date_filed = date(2014, 11, 16)
        d = Docket.objects.create(source=0, court_id="scotus")
        de1 = DocketEntry.objects.create(
            docket=d,
            entry_number=None,
            description=long_desc,
            date_filed=date_filed,
        )
        RECAPDocument.objects.create(
            docket_entry=de1,
            document_number="",
            description="",
            document_type=RECAPDocument.PACER_DOCUMENT,
        )
        de2 = DocketEntry.objects.create(
            docket=d, entry_number=None, description="", date_filed=date_filed
        )
        RECAPDocument.objects.create(
            docket_entry=de2,
            document_number="",
            description=short_desc,
            document_type=RECAPDocument.PACER_DOCUMENT,
        )
        # Add a docket entry that spans the two above. Same date, same short
        # and long description. This should trigger a merge.
        add_docket_entries(
            d,
            [
                {
                    "date_filed": date_filed,
                    "description": long_desc,
                    "document_number": None,
                    "pacer_doc_id": None,
                    "pacer_seq_no": None,
                    "short_description": short_desc,
                }
            ],
        )
        expected_item_count = 1
        self.assertEqual(d.docket_entries.count(), expected_item_count)


class DescriptionCleanupTest(TestCase):
    def test_has_entered_date_at_end(self) -> None:
        desc = "test (Entered: 01/01/2000)"
        docket_entry = {"description": desc}
        normalize_long_description(docket_entry)
        self.assertEqual(docket_entry["description"], "test")

    def test_has_entered_date_in_middle(self) -> None:
        desc = "test (Entered: 01/01/2000) test"
        docket_entry = {"description": desc}
        normalize_long_description(docket_entry)
        self.assertEqual(docket_entry["description"], desc)

    def test_has_entered_date_in_middle_and_end(self) -> None:
        desc = "test (Entered: 01/01/2000) and stuff (Entered: 01/01/2000)"
        docket_entry = {"description": desc}
        normalize_long_description(docket_entry)
        self.assertEqual(
            docket_entry["description"], "test (Entered: 01/01/2000) and stuff"
        )

    def test_has_no_entered_date(self) -> None:
        desc = "test stuff"
        docket_entry = {"description": "test stuff"}
        normalize_long_description(docket_entry)
        self.assertEqual(docket_entry["description"], desc)

    def test_no_description(self) -> None:
        docket_entry = {}
        normalize_long_description(docket_entry)
        self.assertEqual(docket_entry, {})

    def test_removing_brackets(self) -> None:
        docket_entry = {"description": "test [10] stuff"}
        normalize_long_description(docket_entry)
        self.assertEqual(docket_entry["description"], "test 10 stuff")

    def test_only_remove_brackets_on_numbers(self) -> None:
        desc = "test [asdf 10] stuff"
        docket_entry = {"description": desc}
        normalize_long_description(docket_entry)
        self.assertEqual(docket_entry["description"], desc)


class RecapDocketTaskTest(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.get(username="recap")
        self.filename = "cand.html"
        path = os.path.join(
            settings.INSTALL_ROOT, "cl", "recap", "test_assets", self.filename
        )
        with open(path, "rb") as f:
            f = SimpleUploadedFile(self.filename, f.read())
        self.pq = ProcessingQueue.objects.create(
            court_id="scotus",
            uploader=self.user,
            pacer_case_id="asdf",
            filepath_local=f,
            upload_type=UPLOAD_TYPE.DOCKET,
        )

    def tearDown(self) -> None:
        self.pq.filepath_local.delete()
        self.pq.delete()
        Docket.objects.all().delete()

    def test_parsing_docket_does_not_exist(self) -> None:
        """Can we parse an HTML docket we have never seen before?"""
        returned_data = process_recap_docket(self.pq.pk)
        d = Docket.objects.get(pk=returned_data["docket_pk"])
        self.assertEqual(d.source, Docket.RECAP)
        self.assertTrue(d.case_name)
        self.assertEqual(d.jury_demand, "None")

    def test_parsing_docket_already_exists(self) -> None:
        """Can we parse an HTML docket for a docket we have in the DB?"""
        existing_d = Docket.objects.create(
            source=Docket.DEFAULT, pacer_case_id="asdf", court_id="scotus"
        )
        returned_data = process_recap_docket(self.pq.pk)
        d = Docket.objects.get(pk=returned_data["docket_pk"])
        self.assertEqual(d.source, Docket.RECAP_AND_SCRAPER)
        self.assertTrue(d.case_name)
        self.assertEqual(existing_d.pacer_case_id, d.pacer_case_id)

    def test_docket_and_de_already_exist(self) -> None:
        """Can we parse if the docket and the docket entry already exist?"""
        existing_d = Docket.objects.create(
            source=Docket.DEFAULT, pacer_case_id="asdf", court_id="scotus"
        )
        existing_de = DocketEntry.objects.create(
            docket=existing_d, entry_number="1", date_filed=date(2008, 1, 1)
        )
        returned_data = process_recap_docket(self.pq.pk)
        d = Docket.objects.get(pk=returned_data["docket_pk"])
        de = d.docket_entries.get(pk=existing_de.pk)
        self.assertNotEqual(
            existing_de.description,
            de.description,
            msg="Description field did not get updated during import.",
        )
        self.assertTrue(
            de.recap_documents.filter(is_available=False).exists(),
            msg="Recap document didn't get created properly.",
        )
        self.assertTrue(
            d.docket_entries.filter(entry_number="2").exists(),
            msg="New docket entry didn't get created.",
        )

    @mock.patch(
        "cl.lib.storage.get_name_by_incrementing",
        side_effect=clobbering_get_name,
    )
    def test_orphan_documents_are_added(self, mock) -> None:
        """If there's a pq that exists but previously wasn't processed, do we
        clean it up after we finish adding the docket?
        """
        pq = ProcessingQueue.objects.create(
            court_id="scotus",
            uploader=self.user,
            pacer_case_id="asdf",
            pacer_doc_id="03504231050",
            document_number="1",
            filepath_local=SimpleUploadedFile(
                "file.pdf", b"file content more content"
            ),
            upload_type=UPLOAD_TYPE.PDF,
            status=PROCESSING_STATUS.FAILED,
        )
        process_recap_docket(self.pq.pk)
        pq.refresh_from_db()
        self.assertEqual(pq.status, PROCESSING_STATUS.SUCCESSFUL)


class ClaimsRegistryTaskTest(TestCase):
    """Can we handle claims registry uploads?"""

    fixtures = ["canb_court.json"]

    def setUp(self) -> None:
        self.user = User.objects.get(username="recap")
        self.filename = "claims_registry_njb.html"
        path = os.path.join(
            settings.INSTALL_ROOT, "cl", "recap", "test_assets", self.filename
        )
        with open(path, "rb") as f:
            f = SimpleUploadedFile(self.filename, f.read())
        self.pq = ProcessingQueue.objects.create(
            court_id="canb",
            uploader=self.user,
            pacer_case_id="asdf",
            filepath_local=f,
            upload_type=UPLOAD_TYPE.CLAIMS_REGISTER,
        )

    def tearDown(self) -> None:
        self.pq.filepath_local.delete()
        self.pq.delete()
        Docket.objects.all().delete()

    def test_parsing_docket_does_not_exist(self) -> None:
        """Can we parse the claims registry when the docket doesn't exist?"""
        returned_data = process_recap_claims_register(self.pq.pk)
        d = Docket.objects.get(pk=returned_data["docket_pk"])
        self.assertEqual(d.source, Docket.RECAP)
        self.assertTrue(d.case_name)
        expected_claims_count = 7
        self.assertEqual(d.claims.count(), expected_claims_count)

    def test_parsing_bad_data(self) -> None:
        """Can we handle it when there's no data to parse?"""
        filename = "claims_registry_empty.html"
        path = os.path.join(
            settings.INSTALL_ROOT, "cl", "recap", "test_assets", filename
        )
        with open(path, "rb") as f:
            f = SimpleUploadedFile(filename, f.read())
        self.pq.filepath_local = f
        self.pq.save()

        returned_data = process_recap_claims_register(self.pq.pk)
        self.assertIsNone(returned_data)
        self.pq.refresh_from_db()
        self.assertTrue(self.pq.status, PROCESSING_STATUS.INVALID_CONTENT)


class RecapDocketAppellateTaskTest(TestCase):
    fixtures = ["hawaii_court.json"]

    def setUp(self) -> None:
        self.user = User.objects.get(username="recap")
        self.filename = "ca9.html"
        path = os.path.join(
            settings.INSTALL_ROOT, "cl", "recap", "test_assets", self.filename
        )
        with open(path, "rb") as f:
            f = SimpleUploadedFile(self.filename, f.read())
        self.pq = ProcessingQueue.objects.create(
            court_id="scotus",
            uploader=self.user,
            pacer_case_id="asdf",
            filepath_local=f,
            upload_type=UPLOAD_TYPE.APPELLATE_DOCKET,
        )

    def tearDown(self) -> None:
        self.pq.filepath_local.delete()
        self.pq.delete()
        Docket.objects.all().delete()
        OriginatingCourtInformation.objects.all().delete()

    def test_parsing_appellate_docket(self) -> None:
        """Can we parse an HTML docket we have never seen before?"""
        returned_data = process_recap_appellate_docket(self.pq.pk)
        d = Docket.objects.get(pk=returned_data["docket_pk"])
        self.assertEqual(d.source, Docket.RECAP)
        self.assertTrue(d.case_name)
        self.assertEqual(d.appeal_from_id, "hid")
        self.assertIn("Hawaii", d.appeal_from_str)

        # Test the originating court information
        og_info = d.originating_court_information
        self.assertTrue(og_info)
        self.assertIn("Gloria", og_info.court_reporter)
        self.assertEqual(og_info.date_judgment, date(2017, 3, 29))
        self.assertEqual(og_info.docket_number, u"1:17-cv-00050")


class RecapCriminalDataUploadTaskTest(TestCase):
    """Can we handle it properly when criminal data is uploaded as part of
    a docket?
    """

    def setUp(self) -> None:
        self.user = User.objects.get(username="recap")
        self.filename = "cand_criminal.html"
        path = os.path.join(
            settings.INSTALL_ROOT, "cl", "recap", "test_assets", self.filename
        )
        with open(path, "rb") as f:
            f = SimpleUploadedFile(self.filename, f.read())
        self.pq = ProcessingQueue.objects.create(
            court_id="scotus",
            uploader=self.user,
            pacer_case_id="asdf",
            filepath_local=f,
            upload_type=UPLOAD_TYPE.DOCKET,
        )

    def tearDown(self) -> None:
        self.pq.filepath_local.delete()
        self.pq.delete()
        Docket.objects.all().delete()

    def test_criminal_data_gets_created(self) -> None:
        """Does the criminal data appear in the DB properly when we process
        the docket?
        """
        process_recap_docket(self.pq.pk)
        expected_criminal_count_count = 1
        self.assertEqual(
            expected_criminal_count_count, CriminalCount.objects.count()
        )
        expected_criminal_complaint_count = 1
        self.assertEqual(
            expected_criminal_complaint_count,
            CriminalComplaint.objects.count(),
        )


@mock.patch("cl.recap.tasks.add_items_to_solr")
class RecapAttachmentPageTaskTest(TestCase):
    def setUp(self) -> None:
        user = User.objects.get(username="recap")
        self.filename = "cand.html"
        test_dir = os.path.join(
            settings.INSTALL_ROOT, "cl", "recap", "test_assets"
        )
        self.att_filename = "dcd_04505578698.html"
        att_path = os.path.join(test_dir, self.att_filename)
        with open(att_path, "rb") as f:
            self.att = SimpleUploadedFile(self.att_filename, f.read())
        d = Docket.objects.create(
            source=0, court_id="scotus", pacer_case_id="asdf"
        )
        de = DocketEntry.objects.create(docket=d, entry_number=1)
        RECAPDocument.objects.create(
            docket_entry=de,
            document_number="1",
            pacer_doc_id="04505578698",
            document_type=RECAPDocument.PACER_DOCUMENT,
        )
        self.pq = ProcessingQueue.objects.create(
            court_id="scotus",
            uploader=user,
            upload_type=UPLOAD_TYPE.ATTACHMENT_PAGE,
            filepath_local=self.att,
        )

    def tearDown(self) -> None:
        RECAPDocument.objects.filter(
            document_type=RECAPDocument.ATTACHMENT,
        ).delete()

    def test_attachments_get_created(self, mock):
        """Do attachments get created if we have a RECAPDocument to match
        on?"""
        process_recap_attachment(self.pq.pk)
        num_attachments_to_create = 3
        self.assertEqual(
            RECAPDocument.objects.filter(
                document_type=RECAPDocument.ATTACHMENT
            ).count(),
            num_attachments_to_create,
        )
        self.pq.refresh_from_db()
        self.assertEqual(self.pq.status, PROCESSING_STATUS.SUCCESSFUL)

    def test_no_rd_match(self, mock):
        """If there's no RECAPDocument to match on, do we fail gracefully?"""
        RECAPDocument.objects.all().delete()
        with self.assertRaises(RECAPDocument.DoesNotExist):
            process_recap_attachment(self.pq.pk)
        self.pq.refresh_from_db()
        # This doesn't do the celery retries, unfortunately. If we get that
        # working, the correct status is PROCESSING_STATUS.FAILED.
        self.assertEqual(self.pq.status, PROCESSING_STATUS.QUEUED_FOR_RETRY)


class RecapUploadAuthenticationTest(TestCase):
    def setUp(self) -> None:
        self.client = APIClient()
        self.path = reverse("processingqueue-list", kwargs={"version": "v3"})

    def test_authentication(self) -> None:
        """Does POSTing and GETting fail when we send the wrong credentials?"""
        self.client.credentials(HTTP_AUTHORIZATION="Token asdf")  # Junk token.
        r = self.client.post(self.path)
        self.assertEqual(r.status_code, HTTP_401_UNAUTHORIZED)

        r = self.client.get(self.path)
        self.assertEqual(r.status_code, HTTP_401_UNAUTHORIZED)

    def test_no_credentials(self) -> None:
        """Does POSTing and GETting fail if we lack credentials?"""
        self.client.credentials()
        r = self.client.post(self.path)
        self.assertEqual(r.status_code, HTTP_401_UNAUTHORIZED)

        r = self.client.get(self.path)
        self.assertEqual(r.status_code, HTTP_401_UNAUTHORIZED)


class IdbImportTest(TestCase):
    """Assorted tests for the IDB importer."""

    cmd = Command()

    def test_csv_parsing(self) -> None:
        # https://www.ietf.org/rfc/rfc4180.txt
        qa = (
            # Satisfies RFC 4180 rules 1 & 2 (simple values)
            ("asdf\tasdf", {"1": "asdf", "2": "asdf"}),
            # RFC 4180 rule 5 (quotes around value)
            (
                'asdf\t"toyrus"\tasdf',
                {"1": "asdf", "2": "toyrus", "3": "asdf"},
            ),
            # RFC 4180 rule 6 (tab in the value)
            (
                'asdf\t"\ttoyrus"\tasdf',
                {"1": "asdf", "2": "toyrus", "3": "asdf"},
            ),
            # More tabs in the value.
            (
                'asdf\t"\tto\tyrus"\tasdf',
                {"1": "asdf", "2": "toyrus", "3": "asdf"},
            ),
            # MOAR tabs in the value.
            (
                'asdf\t"\tto\tyrus\t"\tasdf',
                {"1": "asdf", "2": "toyrus", "3": "asdf"},
            ),
            # RFC 4180 rule 7 (double quotes in the value)
            (
                'asdf\t"M/V ""Pheonix"""\tasdf',
                {"1": "asdf", "2": 'M/V "Pheonix"', "3": "asdf"},
            ),
        )
        for qa in qa:
            print("Testing CSV parser on: %s" % qa[0])
            self.assertEqual(
                self.cmd.make_csv_row_dict(qa[0], ["1", "2", "3"]), qa[1]
            )
