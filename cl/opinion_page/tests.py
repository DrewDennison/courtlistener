import datetime
import os
import shutil
from unittest import mock
from unittest.mock import MagicMock

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.test.client import Client
from django.urls import reverse
from rest_framework.status import (
    HTTP_200_OK,
    HTTP_300_MULTIPLE_CHOICES,
    HTTP_302_FOUND,
    HTTP_400_BAD_REQUEST,
    HTTP_404_NOT_FOUND,
)

from cl.lib.storage import clobbering_get_name
from cl.lib.test_helpers import SitemapTest
from cl.opinion_page.forms import TennWorkersForm
from cl.opinion_page.views import make_docket_title
from cl.people_db.models import Person
from cl.search.models import (
    SEARCH_TYPES,
    Citation,
    Docket,
    Opinion,
    OpinionCluster,
)


class TitleTest(TestCase):
    def test_make_title_no_docket_number(self) -> None:
        """Can we make titles?"""
        # No docket number
        d = Docket(case_name="foo", docket_number=None)
        self.assertEqual(make_docket_title(d), "foo")


class SimpleLoadTest(TestCase):
    fixtures = [
        "test_objects_search.json",
        "judge_judy.json",
        "recap_docs.json",
    ]

    def test_simple_opinion_page(self) -> None:
        """Does the page load properly?"""
        path = reverse("view_case", kwargs={"pk": 1, "_": "asdf"})
        response = self.client.get(path)
        self.assertEqual(response.status_code, HTTP_200_OK)
        self.assertIn("33 state 1", response.content.decode())

    def test_simple_rd_page(self) -> None:
        path = reverse(
            "view_recap_document",
            kwargs={"docket_id": 1, "doc_num": "1", "slug": "asdf"},
        )
        response = self.client.get(path)
        self.assertEqual(response.status_code, HTTP_200_OK)


class CitationRedirectorTest(TestCase):
    """Tests to make sure that the basic citation redirector is working."""

    fixtures = ["test_objects_search.json", "judge_judy.json"]
    citation = {"reporter": "F.2d", "volume": "56", "page": "9"}

    def assertStatus(self, r, status):
        self.assertEqual(
            r.status_code,
            status,
            msg="Didn't get a {expected} status code. Got {got} "
            "instead.".format(expected=status, got=r.status_code),
        )

    def test_with_and_without_a_citation(self) -> None:
        """Make sure that the url paths are working properly."""
        r = self.client.get(reverse("citation_redirector"))
        self.assertStatus(r, HTTP_200_OK)

        # Are we redirected to the correct place when we use GET or POST?
        r = self.client.get(
            reverse("citation_redirector", kwargs=self.citation), follow=True
        )
        self.assertEqual(r.redirect_chain[0][1], HTTP_302_FOUND)

        r = self.client.post(
            reverse("citation_redirector"), self.citation, follow=True
        )
        self.assertEqual(r.redirect_chain[0][1], HTTP_302_FOUND)

    def test_multiple_results(self) -> None:
        """Do we return a 300 status code when there are multiple results?"""
        # Duplicate the citation and add it to another cluster instead.
        f2_cite = Citation.objects.get(**self.citation)
        f2_cite.pk = None
        f2_cite.cluster_id = 3
        f2_cite.save()
        r = self.client.get(
            reverse("citation_redirector", kwargs=self.citation)
        )
        self.assertStatus(r, HTTP_300_MULTIPLE_CHOICES)
        f2_cite.delete()

    def test_unknown_citation(self) -> None:
        """Do we get a 404 message if we don't know the citation?"""
        r = self.client.get(
            reverse(
                "citation_redirector",
                kwargs={
                    "reporter": "bad-reporter",
                    "volume": "1",
                    "page": "1",
                },
            ),
        )
        self.assertStatus(r, HTTP_404_NOT_FOUND)

    def test_long_numbers(self) -> None:
        """Do really long WL citations work?"""
        r = self.client.get(
            reverse(
                "citation_redirector",
                kwargs={"reporter": "WL", "volume": "2012", "page": "2995064"},
            ),
        )
        self.assertStatus(r, HTTP_404_NOT_FOUND)

    def test_volume_page(self) -> None:
        r = self.client.get(
            reverse("citation_redirector", kwargs={"reporter": "F.2d"})
        )
        self.assertStatus(r, HTTP_200_OK)

    def test_case_page(self) -> None:
        r = self.client.get(
            reverse(
                "citation_redirector",
                kwargs={"reporter": "F.2d", "volume": "56"},
            )
        )
        self.assertStatus(r, HTTP_200_OK)


class ViewRecapDocketTest(TestCase):
    fixtures = ["test_objects_search.json", "judge_judy.json"]

    def test_regular_docket_url(self) -> None:
        """Can we load a regular docket sheet?"""
        r = self.client.get(reverse("view_docket", args=[1, "case-name"]))
        self.assertEqual(r.status_code, HTTP_200_OK)

    def test_recap_docket_url(self) -> None:
        """Can we redirect to a regular docket URL from a recap/uscourts.*
        URL?
        """
        r = self.client.get(
            reverse(
                "redirect_docket_recap",
                kwargs={"court": "test", "pacer_case_id": "666666"},
            ),
            follow=True,
        )
        self.assertEqual(r.redirect_chain[0][1], HTTP_302_FOUND)


class OgRedirectLookupViewTest(TestCase):

    fixtures = ["recap_docs.json"]

    def setUp(self) -> None:
        self.client = Client(HTTP_USER_AGENT="facebookexternalhit")
        self.url = reverse("redirect_og_lookup")

    def test_do_we_404_no_param(self) -> None:
        """Does the view return 404 when no parameters given?"""
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, HTTP_404_NOT_FOUND)

    def test_unknown_doc(self) -> None:
        """Do we redirect to S3 when unknown file path?"""
        r = self.client.get(self.url, {"file_path": "xxx"})
        self.assertEqual(r.status_code, HTTP_302_FOUND)

    @mock.patch("cl.opinion_page.views.make_png_thumbnail_for_instance")
    def test_success_goes_to_view(self, mock: MagicMock) -> None:
        path = (
            "recap/dev.gov.uscourts.txnd.28766/gov.uscourts.txnd.28766.1.0.pdf"
        )
        r = self.client.get(self.url, {"file_path": path})
        self.assertEqual(r.status_code, HTTP_200_OK)
        mock.assert_called_once()


class NewDocketAlertTest(TestCase):
    fixtures = [
        "test_objects_search.json",
        "judge_judy.json",
        "test_court.json",
        "authtest_data.json",
    ]

    def setUp(self) -> None:
        self.assertTrue(
            self.client.login(username="pandora", password="password")
        )

    def test_bad_parameters(self) -> None:
        """If we omit the pacer_case_id and court_id params, do things fail?"""
        r = self.client.get(reverse("new_docket_alert"))
        self.assertEqual(r.status_code, HTTP_400_BAD_REQUEST)

    def test_unknown_docket(self) -> None:
        """What happens if no docket?"""
        r = self.client.get(
            reverse("new_docket_alert"),
            data={"pacer_case_id": "blah", "court_id": "blah"},
        )
        self.assertEqual(r.status_code, HTTP_404_NOT_FOUND)
        self.assertIn("Refresh this Page", r.content.decode())

    def test_all_systems_go(self) -> None:
        """Does everything work with good parameters and good data?"""
        r = self.client.get(
            reverse("new_docket_alert"),
            data={"pacer_case_id": "666666", "court_id": "test"},
        )
        self.assertEqual(r.status_code, HTTP_200_OK)
        self.assertInHTML("Get Docket Alerts", r.content.decode())


class OpinionSitemapTest(SitemapTest):
    def setUp(self) -> None:
        self.sitemap_url = reverse(
            "sitemaps", kwargs={"section": SEARCH_TYPES.OPINION}
        )
        self.item_qs = OpinionCluster.objects.all()

    def test_does_the_sitemap_have_content(self) -> None:
        super(OpinionSitemapTest, self).assert_sitemap_has_content()


@override_settings(
    MEDIA_ROOT=os.path.join(settings.INSTALL_ROOT, "cl/assets/media/test/")
)
@mock.patch(
    "cl.lib.storage.get_name_by_incrementing",
    side_effect=clobbering_get_name,
)
class UploadPublication(TestCase):

    fixtures = ["tenn_test_judges.json", "tennworkcomp_courts.json"]

    def setUp(self) -> None:
        self.client = Client()
        tenn_group = Group.objects.get(name="tenn_work_uploaders")
        self.tenn_user = User.objects.create_user(
            "learned", "learnedhand@scotus.gov", "thehandofjustice"
        )
        self.reg_user = User.objects.create_user(
            "test_user", "test_user@scotus.gov", "simplepassword"
        )

        self.tenn_user.groups.add(tenn_group)

        self.pdf = SimpleUploadedFile(
            "file.pdf",
            b"%PDF-1.trailer<</Root<</Pages<</Kids[<</MediaBox[0 0 3 3]>>]>>>>>>",
            content_type="application/pdf",
        )
        self.png = SimpleUploadedFile(
            "file.png", b"file_content", content_type="image/png"
        )

        qs = Person.objects.filter(positions__court_id="tennworkcompapp")
        self.work_comp_app_data = {
            "case_title": "A Sample Case",
            "lead_author": qs[0].id,
            "second_judge": qs[1].id,
            "third_judge": qs[2].id,
            "docket_number": "2016-231-12332",
            "court_str": "tennworkcompapp",
            "pk": "tennworkcompapp",
            "cite_volume": "2020",
            "cite_reporter": "TN WC App.",
            "cite_page": "1",
            "publication_date": datetime.date(2019, 4, 13),
        }

        self.work_comp_data = {
            "lead_author": Person.objects.filter(
                positions__court_id="tennworkcompcl"
            )[0].id,
            "case_title": "A Sample Case",
            "docket_number": "2016-231-12332",
            "court_str": "tennworkcompcl",
            "pk": "tennworkcompcl",
            "cite_volume": "2020",
            "cite_reporter": "TN WC",
            "cite_page": "1",
            "publication_date": datetime.date(2019, 4, 13),
        }

    def tearDown(self) -> None:
        if os.path.exists(os.path.join(settings.MEDIA_ROOT, "pdf/2019/")):
            shutil.rmtree(os.path.join(settings.MEDIA_ROOT, "pdf/2019/"))
        Docket.objects.all().delete()

    def test_access_upload_page(self, mock) -> None:
        """Can we successfully access upload page with access?"""
        self.client.login(username="learned", password="thehandofjustice")
        response = self.client.get(
            reverse("court_publish_page", args=["tennworkcompcl"])
        )
        self.assertEqual(response.status_code, 200)

    def test_redirect_without_access(self, mock) -> None:
        """Can we successfully redirect individuals without proper access?"""
        self.client.login(username="test_user", password="simplepassword")
        response = self.client.get(
            reverse("court_publish_page", args=["tennworkcompcl"])
        )
        self.assertEqual(response.status_code, 302)

    def test_pdf_upload(self, mock) -> None:
        """Can we upload a PDF and form?"""
        form = TennWorkersForm(
            self.work_comp_data,
            pk="tennworkcompcl",
            files={"pdf_upload": self.pdf},
        )
        form.fields["lead_author"].queryset = Person.objects.filter(
            positions__court_id="tennworkcompcl"
        )
        if form.is_valid():
            form.save()
        self.assertEqual(form.is_valid(), True, form.errors)

    def test_pdf_validation_failure(self, mock) -> None:
        """Can we fail upload documents that are not PDFs?"""
        form = TennWorkersForm(
            self.work_comp_data,
            pk="tennworkcompcl",
            files={"pdf_upload": self.png},
        )
        form.fields["lead_author"].queryset = Person.objects.filter(
            positions__court_id="tennworkcompcl"
        )
        self.assertFalse(form.is_valid(), form.errors)
        self.assertEqual(
            form.errors["pdf_upload"],
            [
                "File extension “png” is not allowed. Allowed "
                "extensions are: pdf."
            ],
        )

    def test_tn_wc_app_upload(self, mock) -> None:
        """Can we test appellate uplading?"""
        form = TennWorkersForm(
            self.work_comp_app_data,
            pk="tennworkcompapp",
            files={"pdf_upload": self.pdf},
        )
        qs = Person.objects.filter(positions__court_id="tennworkcompapp")
        form.fields["lead_author"].queryset = qs
        form.fields["second_judge"].queryset = qs
        form.fields["third_judge"].queryset = qs

        if form.is_valid():
            form.save()
        self.assertEqual(form.is_valid(), True, form.errors)

    def test_required_case_title(self, mock) -> None:
        """Can we validate required testing field case title?"""
        self.work_comp_app_data.pop("case_title")

        form = TennWorkersForm(
            self.work_comp_app_data,
            pk="tennworkcompapp",
            files={"pdf_upload": self.pdf},
        )
        qs = Person.objects.filter(positions__court_id="tennworkcompapp")
        form.fields["lead_author"].queryset = qs
        form.fields["second_judge"].queryset = qs
        form.fields["third_judge"].queryset = qs
        form.is_valid()
        self.assertEqual(
            form.errors["case_title"], ["This field is required."]
        )

    def test_form_save(self, mock) -> None:
        """Can we saves successfully to db?"""

        pre_count = Opinion.objects.all().count()

        form = TennWorkersForm(
            self.work_comp_app_data,
            pk="tennworkcompapp",
            files={"pdf_upload": self.pdf},
        )
        qs = Person.objects.filter(positions__court_id="tennworkcompapp")
        form.fields["lead_author"].queryset = qs
        form.fields["second_judge"].queryset = qs
        form.fields["third_judge"].queryset = qs

        if form.is_valid():
            form.save()

        self.assertEqual(pre_count + 1, Opinion.objects.all().count())

    def test_handle_duplicate_pdf(self, mock) -> None:
        """Can we validate PDF not in system?"""
        d = Docket.objects.create(
            source=Docket.DIRECT_INPUT,
            court_id="tennworkcompcl",
            pacer_case_id=None,
            docket_number="1234123",
            case_name="One v. Two",
        )
        oc = OpinionCluster.objects.create(
            case_name="One v. Two",
            docket=d,
            date_filed=datetime.date(2010, 1, 1),
        )
        Opinion.objects.create(
            cluster=oc,
            type="Lead Opinion",
            sha1="ffe0ec472b16e4e573aa1bbaf2ae358460b5d72c",
        )

        form2 = TennWorkersForm(
            self.work_comp_data,
            pk="tennworkcompcl",
            files={"pdf_upload": self.pdf},
        )
        form2.fields["lead_author"].queryset = Person.objects.filter(
            positions__court_id="tennworkcompcl"
        )
        if form2.is_valid():
            form2.save()

        self.assertIn(
            "Document already in database", form2.errors["pdf_upload"][0]
        )
