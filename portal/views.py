from flask import flash, redirect, render_template, request, session, url_for, jsonify
import requests
import json
import os.path

try:
    # Python 2
    from urllib.parse import urlparse, urlencode, parse_qs
except ImportError:
    # Python 3
    from urlparse import urlparse, parse_qs
    from urllib import urlencode

from portal import app, csrf
from portal import admin, logger
from portal.decorators import authenticated, site_admin
from portal.utils import load_portal_client, get_safe_redirect, flash_message_parser
from portal.connect_api import (
    get_user_info,
    get_user_group_memberships,
    get_multiplex,
    get_user_connect_status,
    get_user_pending_project_requests,
    get_group_info,
    get_group_members,
    delete_group_entry,
    update_user_group_status,
    get_user_access_token,
    domain_name_edgecase,
    get_user_profile,
    get_user_group_status,
)

# Use these four lines on container
import sys
import subprocess
import os
import signal

# Read configurable tokens and endpoints from config file, values must be set
ciconnect_api_token = app.config["CONNECT_API_TOKEN"]
ciconnect_api_endpoint = app.config["CONNECT_API_ENDPOINT"]
mailgun_api_token = app.config["MAILGUN_API_TOKEN"]
# Read Brand Dir from config and insert path to read
MARKDOWN_DIR = app.config["MARKDOWN_DIR"]
sys.path.insert(0, MARKDOWN_DIR)
# Set sys path and import view routes
sys.path.insert(1, "portal/views")
import group_views
import error_handling
import users_groups
import slate_views
import k8s_views
import admin_views
import monitoring_views

@app.route("/webhooks/github", methods=["GET", "POST"])
@csrf.exempt
def webhooks():
    """Endpoint that accepts post requests from Github Webhooks"""

    cmd = """
    cd {}
    git pull origin master
    """.format(MARKDOWN_DIR)

    p = subprocess.Popen(
        cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE,
    )
    out, err = p.communicate()
    print("Return code: {}".format(p.returncode))
    print("Error message: {}".format(err))

    parent_pid = os.getppid()
    print("Parent PID: {}".format(parent_pid))
    os.kill(parent_pid, signal.SIGHUP)

    return out


@app.route("/", methods=["GET"])
def home():
    """Home page - play with it if you must!"""
    with open(MARKDOWN_DIR + "/home_content/home_text_headline.md", "r") as file:
        ht_headline = file.read()
    with open(MARKDOWN_DIR + "/home_content/home_text_rotating.md", "r") as file:
        ht_rotating = file.read()    
    return render_template("home.html", home_text_headline=ht_headline, home_text_rotating=ht_rotating)

def get_about_markdown(domain_name):
    with open(MARKDOWN_DIR + "/about/about.md", "r") as file:
        about = file.read()
    return about

@app.route("/groups/new", methods=["GET", "POST"])
@authenticated
def create_group():
    """Create groups"""
    access_token = get_user_access_token(session)
    query = {"token": access_token}
    print(query)
    if request.method == "GET":
        sciences = requests.get(ciconnect_api_endpoint + "/v1alpha1/fields_of_science")
        sciences = sciences.json()["fields_of_science"]
        return render_template("groups_create.html", sciences=sciences)
    elif request.method == "POST":
        name = request.form["name"]
        email = request.form["email"]
        phone = request.form["phone"]
        description = request.form["description"]
        try:
            # Purpose/Field of Science for CMS will always be High Energy Physics
            field_of_science = request.form["field_of_science"]
        except:
            field_of_science = "High Energy Physics"

        put_group = {
            "apiVersion": "v1alpha1",
            "kind": "Group",
            "metadata": {
                "name": name,
                "field_of_science": field_of_science,
                "email": email,
                "phone": phone,
                "description": description,
            },
        }
        create_group = requests.put(
            ciconnect_api_endpoint + "/v1alpha1/groups/root/subgroups/" + name,
            params=query,
            json=put_group,
        )
        if create_group.status_code == requests.codes.ok:
            flash_message = flash_message_parser("create_group")
            flash(flash_message, "success")
            return redirect(url_for("groups"))
        else:
            err_message = create_group.json()["message"]
            flash("Failed to create group: {}".format(err_message), "warning")
            return redirect(url_for("groups"))


@app.route("/groups/<group_name>/delete", methods=["POST"])
@authenticated
def delete_group(group_name):
    if request.method == "POST":
        r = delete_group_entry(group_name, session)

        if r.status_code == requests.codes.ok:
            flash_message = flash_message_parser("delete_group")
            flash(flash_message, "success")
            return redirect(url_for("groups"))
        else:
            err_message = r.json()["message"]
            flash("Failed to delete group: {}".format(err_message), "warning")
            return redirect(url_for("view_group", group_name=group_name))


@app.route("/groups/<group_name>/add_group_member/<unix_name>", methods=["POST"])
@site_admin
def add_group_member(group_name, unix_name):
    if request.method == "POST":
        # Add user to group by setting user status to active
        status = "active"
        user_status = update_user_group_status(group_name, unix_name, status, session)

        if user_status.status_code == requests.codes.ok:
            flash_message = flash_message_parser("add_group_member")
            flash(flash_message, "success")
        
            try:
                approver = session['unix_name']
                support_email = "noreply@af.uchicago.edu"
                administrators = admin.get_email_list('root.atlas-af.staff')
                subject = "Account approval"
                body = "User %s approved a request from %s to join group %s" %(approver, unix_name, group_name)
                admin.email_users(support_email, administrators, subject, body)
                logger.info("Account approved: %s" %body)
            except:
                logger.error('Error sending email confirming the account approval')

            return redirect(url_for("view_group_members", group_name=group_name))
        else:
            err_message = user_status.json()["message"]
            flash("Failed to add member to group: {}".format(err_message), "warning")
            return redirect(url_for("view_group_members", group_name=group_name))


@app.route("/groups/<group_name>/delete_group_member/<unix_name>", methods=["POST"])
@authenticated
def remove_group_member(group_name, unix_name):
    if request.method == "POST":
        access_token = get_user_access_token(session)
        query = {"token": access_token}
        try:
            message = request.form["denial-message"]
            denial_message = {"message": message}
            remove_user = requests.delete(
                ciconnect_api_endpoint
                + "/v1alpha1/groups/"
                + group_name
                + "/members/"
                + unix_name,
                params=query,
                json=denial_message,
            )
        except:
            remove_user = requests.delete(
                ciconnect_api_endpoint
                + "/v1alpha1/groups/"
                + group_name
                + "/members/"
                + unix_name,
                params=query,
            )

        if remove_user.status_code == requests.codes.ok:
            flash_message = flash_message_parser("remove_group_member")
            flash(flash_message, "success")
            return redirect(url_for("view_group_members", group_name=group_name))
        else:
            err_message = remove_user.json()["message"]
            flash(
                "Failed to remove member from group: {}".format(err_message), "warning"
            )
            return redirect(url_for("view_group_members", group_name=group_name))


@app.route("/groups/<group_name>/admin_group_member/<unix_name>", methods=["POST"])
@authenticated
def admin_group_member(group_name, unix_name):
    if request.method == "POST":
        status = "admin"
        user_status = update_user_group_status(group_name, unix_name, status, session)

        if user_status.status_code == requests.codes.ok:
            flash_message = flash_message_parser("admin_group_member")
            flash(flash_message, "success")
            return redirect(url_for("view_group_members", group_name=group_name))
        else:
            err_message = user_status.json()["message"]
            flash("Failed make member an admin: {}".format(err_message), "warning")
            return redirect(url_for("view_group_members", group_name=group_name))


@app.route("/groups/<group_name>/subgroups/new", methods=["GET", "POST"])
@authenticated
def create_subgroup(group_name):
    access_token = get_user_access_token(session)
    query = {"token": access_token}
    if request.method == "GET":
        sciences = requests.get(ciconnect_api_endpoint + "/v1alpha1/fields_of_science")
        sciences = sciences.json()["fields_of_science"]
        # Get group members
        group_members = get_group_members(group_name, session)
        # Return list of admins of group
        try:
            group_admins = [
                member for member in group_members if member["state"] == "admin"
            ]
        except:
            group_admins = []
        # Check if user status of root connect group specifically
        connect_group = session["url_host"]["unix_name"]
        unix_name = session["unix_name"]
        user_status = get_user_connect_status(unix_name, connect_group)

        # Get group information
        group = get_group_info(group_name, session)
        return render_template(
            "groups_create.html",
            sciences=sciences,
            group_name=group_name,
            group_admins=group_admins,
            user_status=user_status,
            group=group,
        )

    elif request.method == "POST":
        name = request.form["name"]
        display_name = request.form["display-name"]
        email = request.form["email"]
        phone = request.form["phone"]
        description = request.form["description"]
        try:
            # Purpose/Field of Science for CMS will always be High Energy Physics
            field_of_science = request.form["field_of_science"]
        except:
            field_of_science = "High Energy Physics"

        put_query = {
            "apiVersion": "v1alpha1",
            "metadata": {
                "name": name,
                "display_name": display_name,
                "purpose": field_of_science,
                "email": email,
                "phone": phone,
                "description": description,
            },
        }

        r = requests.put(
            ciconnect_api_endpoint
            + "/v1alpha1/groups/"
            + group_name
            + "/subgroup_requests/"
            + name,
            params=query,
            json=put_query,
        )
        full_created_group_name = group_name + "." + name

        # Check if user status of root connect group specifically
        connect_group = session["url_host"]["unix_name"]
        unix_name = session["unix_name"]
        user_status = get_user_connect_status(unix_name, connect_group)

        if r.status_code == requests.codes.ok:
            if user_status == "admin":
                flash_message = flash_message_parser("create_subgroup")
                flash(flash_message, "success")
                return redirect(
                    url_for("view_group", group_name=full_created_group_name)
                )
            else:
                flash(
                    "The support team has been notified of your requested subgroup.",
                    "success",
                )
                return redirect(url_for("users_groups_pending"))
        else:
            err_message = r.json()["message"]
            flash(
                "Failed to request project creation: {}".format(err_message), "warning"
            )
            return redirect(
                url_for("view_group_subgroups_requests", group_name=group_name)
            )


@app.route("/groups/<group_name>/requests/edit", methods=["GET", "POST"])
@authenticated
def edit_subgroup_requests(group_name):
    access_token = get_user_access_token(session)
    query = {"token": access_token}
    enclosing_group_name = ".".join(group_name.split(".")[:-1])
    if request.method == "GET":
        sciences = requests.get(ciconnect_api_endpoint + "/v1alpha1/fields_of_science")
        sciences = sciences.json()["fields_of_science"]

        group = get_group_info(group_name, session)

        return render_template(
            "groups_requests_edit.html",
            sciences=sciences,
            group_name=group_name,
            group=group,
        )

    elif request.method == "POST":
        name = request.form["name"]
        display_name = request.form["display-name"]
        email = request.form["email"]
        phone = request.form["phone"]
        description = request.form["description"]

        new_unix_name = enclosing_group_name + "." + name

        if new_unix_name == group_name:
            put_query = {
                "apiVersion": "v1alpha1",
                "metadata": {
                    "display_name": display_name,
                    "email": email,
                    "phone": phone,
                    "description": description,
                },
            }
        else:
            put_query = {
                "apiVersion": "v1alpha1",
                "metadata": {
                    "name": name,
                    "display_name": display_name,
                    "email": email,
                    "phone": phone,
                    "description": description,
                },
            }

        r = requests.put(
            ciconnect_api_endpoint + "/v1alpha1/groups/" + group_name,
            params=query,
            json=put_query,
        )

        enclosing_group_name = ".".join(group_name.split(".")[:-1])
        if r.status_code == requests.codes.ok:
            flash_message = flash_message_parser("edit_subgroup_requests")
            flash(flash_message, "success")
            return redirect(url_for("users_groups_pending"))
        else:
            err_message = r.json()["message"]
            flash("Failed to edit subgroup request: {}".format(err_message), "warning")
            return redirect(
                url_for(
                    "edit_subgroup_requests",
                    group_name=group_name,
                    name=name,
                    display_name=display_name,
                    email=email,
                    phone=phone,
                    description=description,
                )
            )


@app.route("/groups/<group_name>/edit", methods=["GET", "POST"])
@authenticated
def edit_subgroup(group_name):
    access_token = get_user_access_token(session)
    query = {"token": access_token}
    if request.method == "GET":
        sciences = requests.get(ciconnect_api_endpoint + "/v1alpha1/fields_of_science")
        sciences = sciences.json()["fields_of_science"]
        group = get_group_info(group_name, session)
        return render_template(
            "groups_edit.html", sciences=sciences, group_name=group_name, group=group
        )

    elif request.method == "POST":
        display_name = request.form["display-name"]
        email = request.form["email"]
        phone = request.form["phone"]
        description = request.form["description"]

        put_query = {
            "apiVersion": "v1alpha1",
            "metadata": {
                "display_name": display_name,
                "email": email,
                "phone": phone,
                "description": description,
            },
        }

        r = requests.put(
            ciconnect_api_endpoint + "/v1alpha1/groups/" + group_name,
            params=query,
            json=put_query,
        )

        if r.status_code == requests.codes.ok:
            flash_message = flash_message_parser("edit_subgroup")
            flash(flash_message, "success")
            return redirect(url_for("view_group", group_name=group_name))
        else:
            err_message = r.json()["message"]
            flash(
                "Failed to update subgroup information: {}".format(err_message),
                "warning",
            )
            return redirect(url_for("edit_subgroup", group_name=group_name))


@app.route("/groups/<group_name>/subgroups/<subgroup_name>/approve", methods=["GET"])
@authenticated
def approve_subgroup(group_name, subgroup_name):
    access_token = get_user_access_token(session)
    query = {"token": access_token}
    if request.method == "GET":

        r = requests.put(
            ciconnect_api_endpoint
            + "/v1alpha1/groups/"
            + group_name
            + "/subgroup_requests/"
            + subgroup_name
            + "/approve",
            params=query,
        )

        if r.status_code == requests.codes.ok:
            flash_message = flash_message_parser("approve_subgroup")
            flash(flash_message, "success")
            return redirect(
                url_for("view_group_subgroups_requests", group_name=group_name)
            )
        else:
            err_message = r.json()["message"]
            flash(
                "Failed to approve subgroup creation: {}".format(err_message), "warning"
            )
            return redirect(
                url_for("view_group_subgroups_requests", group_name=group_name)
            )


@app.route("/groups/<group_name>/subgroups/<subgroup_name>/deny", methods=["POST"])
@authenticated
def deny_subgroup(group_name, subgroup_name):
    access_token = get_user_access_token(session)
    query = {"token": access_token}
    if request.method == "POST":
        message = request.form["denial-message"]
        denial_message = {"message": message}

        r = requests.delete(
            ciconnect_api_endpoint
            + "/v1alpha1/groups/"
            + group_name
            + "/subgroup_requests/"
            + subgroup_name,
            params=query,
            json=denial_message,
        )

        if r.status_code == requests.codes.ok:
            flash_message = flash_message_parser("deny_subgroup")
            flash(flash_message, "success")
            return redirect(
                url_for("view_group_subgroups_requests", group_name=group_name)
            )
        else:
            err_message = r.json()["message"]
            flash("Failed to deny subgroup request: {}".format(err_message), "warning")
            return redirect(
                url_for("view_group_subgroups_requests", group_name=group_name)
            )


@app.route("/signup", methods=["GET"])
def signup():
    """Send the user to Globus Auth with signup=1."""
    with open(MARKDOWN_DIR + "/signup_content/signup_modal.md", "r") as file:
        signup_modal_md = file.read()
    with open(MARKDOWN_DIR + "/signup_content/signup_instructions.md", "r") as file:
        signup_instructions_md = file.read()
    with open(MARKDOWN_DIR + "/signup_content/signup.md", "r") as file:
        signup_md = file.read()
    return render_template("signup.html", 
        signup_modal_md=signup_modal_md, signup_instructions_md=signup_instructions_md, signup_md=signup_md)


@app.route("/aup", methods=["GET"])
def aup():
    """Send the user to Acceptable Use Policy page"""
    with open(MARKDOWN_DIR + "/signup_content/signup_modal.md", "r") as file:
        aup_md = file.read()
    return render_template("AUP.html", aup_md=aup_md)

@app.route("/hardware", methods=["GET"])
def hardware_information():
    return render_template("hardware.html")

@app.route("/about", methods=["GET"])
def about():
    """Send the user to the About page"""
    with open(MARKDOWN_DIR + "/about/about.md", "r") as file:
        about = file.read()

    return render_template("about.html", about=about)


@app.route("/login", methods=["GET"])
def login():
    """Send the user to Globus Auth."""
    next_url = get_safe_redirect()
    return redirect(url_for("authcallback", next=next_url))


@app.route("/logout", methods=["GET"])
@authenticated
def logout():
    """
    - Revoke the tokens with Globus Auth.
    - Destroy the session state.
    - Redirect the user to the Globus Auth logout page.
    """
    client = load_portal_client()

    # Revoke the tokens with Globus Auth
    for token, token_type in (
        (token_info[ty], ty)
        # get all of the token info dicts
        for token_info in session["tokens"].values()
        # cross product with the set of token types
        for ty in ("access_token", "refresh_token")
        # only where the relevant token is actually present
        if token_info[ty] is not None
    ):
        client.oauth2_revoke_token(
            token, additional_params={"token_type_hint": token_type}
        )

    # Destroy the session state
    session.clear()

    redirect_uri = url_for("home", _external=True)

    ga_logout_url = []
    ga_logout_url.append(app.config["GLOBUS_AUTH_LOGOUT_URI"])
    ga_logout_url.append("?client={}".format(app.config["PORTAL_CLIENT_ID"]))
    ga_logout_url.append("&redirect_uri={}".format(redirect_uri))
    ga_logout_url.append("&redirect_name=Connect Portal")

    # Redirect the user to the Globus Auth logout page
    # return redirect("".join(ga_logout_url))
    return redirect(url_for("home"))


@app.route("/profile/new", methods=["GET", "POST"])
@authenticated
def create_profile():
    identity_id = session.get("primary_identity")
    institution = session.get("institution")
    globus_id = identity_id
    query = {"token": ciconnect_api_token}

    if request.method == "GET":
        unix_name = ""
        phone = ""
        public_key = ""
        return render_template(
            "profile_create.html",
            unix_name=unix_name,
            phone=phone,
            public_key=public_key,
        )

    if request.method == "POST":
        name = request.form["name"]
        unix_name = request.form["unix_name"]
        email = request.form["email"]
        phone = request.form["phone-number"]
        institution = request.form["institution"]
        public_key = request.form["sshpubstring"]
        globus_id = session["primary_identity"]
        superuser = False
        service_account = False

        if public_key:
            post_user = {
                "apiVersion": "v1alpha1",
                "metadata": {
                    "globusID": globus_id,
                    "name": name,
                    "email": email,
                    "phone": phone,
                    "institution": institution,
                    "public_key": public_key,
                    "unix_name": unix_name,
                    "superuser": superuser,
                    "service_account": service_account,
                },
            }
        else:
            post_user = {
                "apiVersion": "v1alpha1",
                "metadata": {
                    "globusID": globus_id,
                    "name": name,
                    "email": email,
                    "phone": phone,
                    "institution": institution,
                    "unix_name": unix_name,
                    "superuser": superuser,
                    "service_account": service_account,
                },
            }
        try:
            r = requests.post(ciconnect_api_endpoint + "/v1alpha1/users", params=query, json=post_user)
            if r.status_code == requests.codes.ok:
                r = r.json()["metadata"]
                session["name"] = r["name"]
                session["email"] = r["email"]
                session["phone"] = r["phone"]
                session["institution"] = r["institution"]
                session["unix_name"] = r["unix_name"]

                group_name = session["url_host"]["unix_name"]
                status = "pending"
                update_user_group_status(group_name, unix_name, status, session)

                flash_message = flash_message_parser("create_profile")
                flash(flash_message, "success")

                if "next" in session:
                    redirect_to = session["next"]
                    session.pop("next")
                else:
                    redirect_to = url_for("profile")
                return redirect(url_for("profile"))
            else:
                error_msg = r.json()["message"]
                flash("Failed to create your account: {}".format(error_msg), "warning")
                return render_template(
                    "profile_create.html",
                    name=name,
                    unix_name=unix_name,
                    email=email,
                    phone=phone,
                    institution=institution,
                    public_key=public_key,
                )
        except:
            flash("Error calling connect-api", "warning")
            return render_template(
                "profile_create.html",
                name=name,
                unix_name=unix_name,
                email=email,
                phone=phone,
                institution=institution,
                public_key=public_key
            )



@app.route("/profile/edit/<unix_name>", methods=["GET", "POST"])
@authenticated
def edit_profile(unix_name):
    identity_id = session.get("primary_identity")
    query = {"token": ciconnect_api_token, "globus_id": identity_id}
    user = get_user_info(session)
    expected_unix_name = user["metadata"]["unix_name"]

    try:
        unix_name == expected_unix_name
    except Exception:
        return redirect(url_for("handle_exception", e=Exception))

    if request.method == "GET":
        # Get user info, pass through as args, convert to json and load input fields
        profile = get_user_profile(unix_name)
        profile = profile["metadata"]

        return render_template(
            "profile_edit.html", profile=profile, unix_name=unix_name
        )

    elif request.method == "POST":
        name = request.form["name"]
        email = request.form["email"]
        phone = request.form["phone-number"]
        institution = request.form["institution"]
        public_key = request.form["sshpubstring"]
        globus_id = session["primary_identity"]
        x509dn = request.form["x509dn"]
        access_token = get_user_access_token(session)
        query = {"token": access_token, "globus_id": identity_id}
        # Schema and query for adding users to CI Connect DB
        if public_key != " ":
            post_user = {
                "apiVersion": "v1alpha1",
                "metadata": {
                    "name": name,
                    "email": email,
                    "phone": phone,
                    "institution": institution,
                    "public_key": public_key,
                    "X.509_DN": x509dn,
                },
            }
        else:
            post_user = {
                "apiVersion": "v1alpha1",
                "metadata": {
                    "name": name,
                    "email": email,
                    "phone": phone,
                    "institution": institution,
                    "X.509_DN": x509dn,
                },
            }
        # PUT request to update user information
        r = requests.put(
            ciconnect_api_endpoint + "/v1alpha1/users/" + unix_name,
            params=query,
            json=post_user,
        )

        session["name"] = name
        session["email"] = email
        session["phone"] = phone
        session["institution"] = institution

        flash_message = flash_message_parser("edit_profile")
        flash(flash_message, "success")

        if "next" in session:
            redirect_to = session["next"]
            session.pop("next")
        else:
            redirect_to = url_for("profile")

        return redirect(url_for("profile"))


@app.route("/profile", methods=["GET"])
@authenticated
def profile():
    """User profile information. Assocated with a Globus Auth identity."""
    if request.method == "GET":
        identity_id = session.get("primary_identity")
        try:
            user = get_user_info(session)
            unix_name = user["metadata"]["unix_name"]
            profile = get_user_profile(unix_name)
        except:
            profile = None

        if profile:
            # print("Found profile: {}".format(profile))
            profile = profile["metadata"]
            unix_name = profile["unix_name"]
            group_name = session["url_host"]["unix_name"]
            user_status = get_user_group_status(unix_name, group_name, session)
        else:
            flash(
                "Please complete any missing profile fields and press Save.", "warning"
            )
            return redirect(url_for("create_profile"))

        if request.args.get("next"):
            session["next"] = get_safe_redirect()

        group_memberships = []
        for group in profile["group_memberships"]:
            if (session["url_host"]["unix_name"] in group["name"]) and (
                len(group["name"].split(".")) > 1
            ):
                group_memberships.append(group)

        with open(MARKDOWN_DIR + "/form_descriptions/group_unix_name_description.md", "r") as file:
            group_unix_name_description = file.read()

        return render_template(
            "profile.html",
            profile=profile,
            user_status=user_status,
            group_memberships=group_memberships,
            group_unix_name_description=group_unix_name_description
        )

@app.route("/authcallback", methods=["GET"])
def authcallback():
    """Handles the interaction with Globus Auth."""
    # If we're coming back from Globus Auth in an error state, the error
    # will be in the "error" query string parameter.
    if "error" in request.args:
        flash(
            "You could not be logged into the portal: "
            + request.args.get("error_description", request.args["error"]),
            "warning",
        )
        return redirect(url_for("home"))

    # Set up our Globus Auth/OAuth2 state
    redirect_uri = url_for("authcallback", _external=True)
    if 'https://' not in redirect_uri:
        redirect_uri = redirect_uri.replace("http://", "https://", 1)

    client = load_portal_client()
    client.oauth2_start_flow(redirect_uri, refresh_tokens=True)

    # If there's no "code" query string parameter, we're in this route
    # starting a Globus Auth login flow.
    if "code" not in request.args:
        # print("SIGNUP: {} ".format(request.args))
        next_url = get_safe_redirect()
        additional_authorize_params = (
            {"signup": 1} if request.args.get("signup") else {"next": next_url}
        )

        auth_uri = client.oauth2_get_authorize_url(
            additional_params=additional_authorize_params
        )
        print("ADDITIONAL AUTHORIZED PARAMS: {}".format(additional_authorize_params))
        print("NEXT URL: {}".format(next_url))

        return redirect(auth_uri)
    else:
        # If we do have a "code" param, we're coming back from Globus Auth
        # and can start the process of exchanging an auth code for a token.
        print("GOT OUT OF AUTH URI LOOP")
        next_url = get_safe_redirect()
        print("NEXT URL: {}".format(next_url))
        code = request.args.get("code")
        tokens = client.oauth2_exchange_code_for_tokens(code)

        id_token = tokens.decode_id_token(client)
        session.update(
            tokens=tokens.by_resource_server,
            is_authenticated=True,
            name=id_token.get("name", ""),
            email=id_token.get("email", ""),
            institution=id_token.get("organization", ""),
            primary_username=id_token.get("preferred_username"),
            primary_identity=id_token.get("sub"),
        )

        access_token = session["tokens"]["auth.globus.org"]["access_token"]
        token_introspect = client.oauth2_token_introspect(
            token=access_token, include="identity_set"
        )
        identity_set = token_introspect.data["identity_set"]
        profile = None

        for identity in identity_set:
            query = {"token": ciconnect_api_token, "globus_id": identity}
            try:
                r = requests.get(
                    ciconnect_api_endpoint + "/v1alpha1/find_user", params=query
                )
                # r = get_user_info(session)
                if r.status_code == requests.codes.ok:
                    user_info = r.json()
                    # user_access_token = user_info['metadata']['access_token']
                    unix_name = user_info["metadata"]["unix_name"]
                    profile = requests.get(
                        ciconnect_api_endpoint + "/v1alpha1/users/" + unix_name,
                        params=query,
                    )
                    profile = profile.json()
                    session["primary_identity"] = identity
            except:
                print("NO PROFILE FOUND WITH IDENTITY: {}".format(identity))

        try:
            referrer = urlparse(request.referrer)
            # print("REFERRER: {}".format(referrer))
            queries = parse_qs(referrer.query)
            # print("QUERIES: {}".format(queries))
            redirect_uri = queries["redirect_uri"][0]
            # print("REDIRECT URI: {}".format(redirect_uri))
            next_url = queries["next"][0]
            # print("AFTER QUERIES NEXT URL: {}".format(next_url))
        except:
            next_url = "/"

        if profile:
            profile = profile["metadata"]
            session["name"] = profile["name"]
            session["email"] = profile["email"]
            session["phone"] = profile["phone"]
            session["institution"] = profile["institution"]
            session["unix_name"] = profile["unix_name"]
            session["url_root"] = request.url_root
            session["url_host"] = {
                "name": "atlas-af",
                "display_name": "ATLAS Analysis Facility",
                "unix_name": "root.atlas-af"
            }
            session["admin"] = admin_check(profile["unix_name"])
            session["site_member"] = False
            for group in profile["group_memberships"]:
                if group["name"] == "root.atlas-af":
                    session["site_member"] = True
        else:
            session["url_root"] = request.url_root
            return redirect(url_for("create_profile", next=url_for("profile")))

        # print("FINAL NEXT URL: {}".format(next_url))
        if next_url == "/":
            return redirect(url_for("profile"))
        else:
            return redirect(next_url)

def admin_check(unix_name):
    """
    Check user status on login, and set return admin status
    :param unix_name: unix name of user
    :return: user's status in Connect group
    """
    query = {"token": ciconnect_api_token}
    # Query to return user's membership status in a group specifically the root connect group
    r = requests.get(
        ciconnect_api_endpoint
        + "/v1alpha1/users/"
        + unix_name
        + "/groups/"
        + session["url_host"]["unix_name"],
        params=query,
    )
    user_status = r.json()["membership"]["state"]
    return user_status
