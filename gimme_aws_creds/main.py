import configparser
import os
import re
import sys
from os.path import expanduser

import boto3
from okta.framework.ApiClient import ApiClient
from okta.framework.OktaError import OktaError

from gimme_aws_creds.config import Config
from gimme_aws_creds.okta import OktaClient


class GimmeAWSCreds(object):
    """
       This is a CLI tool that gets temporary AWS credentials
       from Okta based the available AWS Okta Apps and roles
       assigned to the user. The user is able to select the app
       and role from the CLI or specify them in a config file by
       passing --configure to the CLI too.
       gimme_aws_creds will either write the credentials to stdout
       or ~/.aws/credentials depending on what was specified when
       --configure was ran.

       Usage:
         -h, --help     show this help message and exit
         --username USERNAME, -u USERNAME
                        The username to use when logging into Okta. The
                        username can also be set via the OKTA_USERNAME env
                        variable. If not provided you will be prompted to
                        enter a username.
         -k, --insecure Allow connections to SSL sites without cert verification
         -c, --configure
                        If set, will prompt user for configuration
                        parameters and then exit.
         --profile PROFILE, -p PROFILE
                        If set, the specified configuration profile will
                        be used instead of the default profile.

        Config Options:
           okta_org_url = Okta URL
           gimme_creds_server = URL of the gimme-creds-server
           client_id = OAuth Client id for the gimme-creds-server
           okta_auth_server = Server ID for the OAuth authorization server used by gimme-creds-server
           write_aws_creds = Option to write creds to ~/.aws/credentials
           cred_profile = Use DEFAULT or Role as the profile in ~/.aws/credentials
           aws_appname = (optional) Okta AWS App Name
           aws_rolename =  (optional) Okta Role Name
    """
    FILE_ROOT = expanduser("~")
    AWS_CONFIG = FILE_ROOT + '/.aws/credentials'

    def __init__(self):
        self.idp_arn = None
        self.role_arn = None

    #  this is modified code from https://github.com/nimbusscale/okta_aws_login
    def _write_aws_creds(self, profile, access_key, secret_key, token):
        """ Writes the AWS STS token into the AWS credential file"""
        # Check to see if the aws creds path exists, if not create it
        creds_dir = os.path.dirname(self.AWS_CONFIG)
        if os.path.exists(creds_dir) is False:
            os.makedirs(creds_dir)
        config = configparser.RawConfigParser()

        # Read in the existing config file if it exists
        if os.path.isfile(self.AWS_CONFIG):
            config.read(self.AWS_CONFIG)

        # Put the credentials into a saml specific section instead of clobbering
        # the default credentials
        if not config.has_section(profile):
            config.add_section(profile)

        config.set(profile, 'aws_access_key_id', access_key)
        config.set(profile, 'aws_secret_access_key', secret_key)
        config.set(profile, 'aws_session_token', token)

        # Write the updated config file
        with open(self.AWS_CONFIG, 'w+') as configfile:
            config.write(configfile)

    def _get_sts_creds(self, assertion, duration=3600):
        """ using the assertion and arns return aws sts creds """
        client = boto3.client('sts')

        response = client.assume_role_with_saml(
            RoleArn=self.role_arn,
            PrincipalArn=self.idp_arn,
            SAMLAssertion=assertion,
            DurationSeconds=duration
        )

        return response['Credentials']

    @staticmethod
    def _call_gimme_creds_server(okta_connection, gimme_creds_server_url):
        """ Retrieve the user's AWS accounts from the gimme_creds_server"""
        response = okta_connection.get(gimme_creds_server_url)

        # Throw an error if we didn't get any accounts back
        if not response.json():
            print("No AWS accounts found.")
            exit()

        return response.json()

    @staticmethod
    def _get_aws_account_info(okta_org_url, okta_api_key, username):
        """ Call the Okta User and App APIs and process the results to return
        just the information we need for gimme_aws_creds"""
        # We need access to the entire JSON response from the Okta APIs, so we need to
        # use the low-level ApiClient instead of UsersClient and AppInstanceClient
        users_client = ApiClient(okta_org_url, okta_api_key, pathname='/api/v1/users')
        app_client = ApiClient(okta_org_url, okta_api_key, pathname='/api/v1/apps')

        # Get User information
        try:
            result = users_client.get_path('/{0}'.format(username))
            user = result.json()
        except OktaError as e:
            if e.error_code == 'E0000007':
                print("Error: " + username + " was not found!")
                exit(1)
            else:
                print("Error: " + e.error_summary)
                exit(1)

        # Get a list of apps for this user and include extended info about the user
        params = {
            'limit': 50,
            'filter': 'user.id+eq+%22' + user['id'] + '%22&expand=user%2F' + user['id']
        }

        try:
            # Get first page of results
            result = app_client.get_path('/', params=params)
            final_result = result.json()

            # Loop through other pages
            while 'next' in result.links:
                print('.', end='', flush=True)
                result = app_client.get(result.links['next']['url'])
                final_result = final_result + result.json()
            print("done\n")
        except OktaError as e:
            if e.error_code == 'E0000007':
                print("Error: No applications found for " + username)
                exit(1)
            else:
                print("Error: " + e.error_summary)
                exit(1)

        # Loop through the list of apps and filter it down to just the info we need
        app_list = []
        for app in final_result:
            # All AWS connections have the same app name
            if app['name'] == 'amazon_aws':
                new_app_entry = {
                    'id': app['id'],
                    'name': app['label'],
                    'identityProviderArn': app['settings']['app']['identityProviderArn'],
                    'roles': []
                }
                # Build a list of the roles this user has access to
                for role in app['_embedded']['user']['profile']['samlRoles']:
                    role_info = {
                        'name': role,
                        'arn': re.sub(
                            ':saml-provider.*', ':role/' + role, app['settings']['app']['identityProviderArn']
                        )
                    }
                    # We can figure out the role ARN based on the ARN for the IdP
                    new_app_entry['roles'].append(role_info)
                new_app_entry['links'] = {}
                new_app_entry['links']['appLink'] = app['_links']['appLinks'][0]['href']
                new_app_entry['links']['appLogo'] = app['_links']['logo'][0]['href']
                app_list.append(new_app_entry)

        # Throw an error if we didn't get any accounts back
        if not app_list:
            print("No AWS accounts found.")
            exit()

        return app_list

    def _choose_app(self, aws_info):
        """ gets a list of available apps and
        ask the user to select the app they want
        to assume a roles for and returns the selection
        """
        if not aws_info:
            return None

        app_strs = []
        for i, app in enumerate(aws_info):
            app_strs.append('[{}] {}'.format(i, app["name"]))

        if app_strs:
            print("Pick an app:")
            # print out the apps and let the user select
            for app in app_strs:
                print(app)
        else:
            return None

        selection = self._get_user_int_selection(0, len(aws_info)-1)

        if selection is None:
            print("You made an invalid selection")
            exit(1)

        return aws_info[int(selection)]

    @staticmethod
    def _get_app_by_name(aws_info, appname):
        """ returns the app with the matching name"""
        for i, app in enumerate(aws_info):
            if app["name"] == appname:
                return app

    @staticmethod
    def _get_role_by_name(app_info, rolename):
        """ returns the role with the matching name"""
        for i, role in enumerate(app_info['roles']):
            if role["name"] == rolename:
                return role

    def _choose_role(self, app_info):
        """ gets a list of available roles and
        asks the user to select the role they want to assume
        """
        if not app_info:
            return None

        # Gather the roles available to the user.
        role_strs = []
        for i, role in enumerate(app_info['roles']):
            if not role:
                continue
            role_strs.append('[{}] {}'.format(i, role["name"]))

        if role_strs:
            print("Pick a role:")
            for role in role_strs:
                print(role)
        else:
            return None

        selection = self._get_user_int_selection(0, len(app_info['roles'])-1)

        if selection is None:
            print("You made an invalid selection")
            exit(1)

        return app_info['roles'][int(selection)]

    @staticmethod
    def _get_user_int_selection(min_int, max_int, max_retries=5):
        selection = None
        for i in range(0, max_retries):
            try:
                selection = int(input("Selection: "))
                break
            except ValueError:
                print('Invalid selection, must be an integer value.')

        if selection is None:
            return None

        # make sure the choice is valid
        if selection < min_int or selection > max_int:
            return None

        return selection

    def run(self):
        """ Pulling it all together to make the CLI """
        config = Config()
        config.get_args()
        # Create/Update config when configure arg set
        if config.configure is True:
            config.update_config_file()
            exit()

        # get the config dict
        conf_dict = config.get_config_dict()

        if not conf_dict.get('okta_org_url'):
            print('No Okta organization URL in configuration.  Try running --config again.')
            exit(1)

        if not conf_dict.get('gimme_creds_server'):
            print('No Gimme-Creds server URL in configuration.  Try running --config again.')
            exit(1)

        okta = OktaClient(conf_dict['okta_org_url'], config.verify_ssl_certs)
        if config.username is not None:
            okta.set_username(config.username)

        # Call the Okta APIs and proces data locally
        if conf_dict.get('gimme_creds_server') == 'internal':
            # Okta API key is required when calling Okta APIs internally
            if config.api_key is None:
                print('OKTA_API_KEY environment variable not found!')
                exit(1)
            # Authenticate with Okta
            auth_result = okta.auth_session()

            print("Authentication Success! Getting AWS Accounts", end='', flush=True)
            aws_results = self._get_aws_account_info(conf_dict['okta_org_url'], config.api_key, auth_result['username'])

        # Use the gimme_creds_lambda service
        else:
            if not conf_dict.get('client_id'):
                print('No OAuth Client ID in configuration.  Try running --config again.')
            if not conf_dict.get('okta_auth_server'):
                print('No OAuth Authorization server in configuration.  Try running --config again.')

            # Authenticate with Okta and get an OAuth access token
            okta.auth_oauth(
                conf_dict['client_id'],
                authorization_server=conf_dict['okta_auth_server'],
                access_token=True,
                id_token=False,
                scopes=['openid']
            )

            # Add Access Tokens to Okta-protected requests
            okta.use_oauth_access_token(True)

            print("Authentication Success! Calling Gimme-Creds Server...")
            aws_results = self._call_gimme_creds_server(okta, conf_dict['gimme_creds_server'])

        # check to see if appname and rolename are set
        # in the config, if not give user a selection to pick from
        if not conf_dict.get('aws_appname'):
            aws_app = self._choose_app(aws_results)
        else:
            aws_app = self._get_app_by_name(
                aws_results, conf_dict['aws_appname'])

        if not aws_app:
            print('AWS app {} not found for this user.'.format(conf_dict['aws_appname']))
            exit(1)

        if not conf_dict.get('aws_rolename'):
            aws_role = self._choose_role(aws_app)
        else:
            aws_role = self._get_role_by_name(
                aws_app, conf_dict['aws_rolename'])

        if not aws_role:
            print('No roles available to this user.')
            exit(1)

        # Get the the identityProviderArn from the aws app
        self.idp_arn = aws_app['identityProviderArn']

        # Get the role ARNs
        self.role_arn = aws_role['arn']

        saml_data = okta.get_saml_response(aws_app['links']['appLink'])
        aws_creds = self._get_sts_creds(saml_data['SAMLResponse'])

        # check if write_aws_creds is true if so
        # get the profile name and write out the file
        if str(conf_dict['write_aws_creds']) == 'True':
            print('writing to ', self.AWS_CONFIG)
            # set the profile name
            if conf_dict['cred_profile'].lower() == 'default':
                profile_name = 'default'
            elif conf_dict['cred_profile'].lower() == 'role':
                profile_name = conf_dict['aws_rolename']
            else:
                profile_name = conf_dict['cred_profile']

            # Write out the AWS Config file
            self._write_aws_creds(
                profile_name,
                aws_creds['AccessKeyId'],
                aws_creds['SecretAccessKey'],
                aws_creds['SessionToken']
            )
        else:
            # Print out temporary AWS credentials.  Credentials are printed to stderr to simplify
            # redirection for use in automated scripts
            print("export AWS_ACCESS_KEY_ID=" + aws_creds['AccessKeyId'], file=sys.stderr)
            print("export AWS_SECRET_ACCESS_KEY=" + aws_creds['SecretAccessKey'], file=sys.stderr)
            print("export AWS_SESSION_TOKEN=" + aws_creds['SessionToken'], file=sys.stderr)

        config.clean_up()