import json
import time
import urllib.parse
from decimal import Decimal
from http import HTTPStatus

from requests import Session

from steampy.confirmation import ConfirmationExecutor
from steampy.exceptions import ApiException, TooManyRequests
from steampy.models import Currency, GameOptions, SteamUrl
from steampy.utils import (
    get_listing_id_to_assets_address_from_html,
    get_market_listings_from_html,
    get_market_sell_listings_from_api,
    login_required,
    merge_items_with_descriptions_from_listing,
    text_between,
)


class SteamMarket:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._steam_guard = None
        self._session_id = None
        self.was_login_executed = False

    def _set_login_executed(self, steamguard: dict, session_id: str) -> None:
        self._steam_guard = steamguard
        self._session_id = session_id
        self.was_login_executed = True

    def fetch_price(
        self, item_hash_name: str, game: GameOptions, currency: Currency = Currency.USD, country='PL',
    ) -> dict:
        url = f'{SteamUrl.COMMUNITY_URL}/market/priceoverview/'
        params = {
            'country': country,
            'currency': currency.value,
            'appid': game.app_id,
            'market_hash_name': item_hash_name,
        }

        response = self._session.get(url, params=params)
        if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
            raise TooManyRequests('You can fetch maximum 20 prices in 60s period')

        return response.json()

    @login_required
    def fetch_price_history(self, item_hash_name: str, game: GameOptions) -> dict:
        url = f'{SteamUrl.COMMUNITY_URL}/market/pricehistory/'
        params = {'country': 'PL', 'appid': game.app_id, 'market_hash_name': item_hash_name}

        response = self._session.get(url, params=params)
        if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
            raise TooManyRequests('You can fetch maximum 20 prices in 60s period')

        return response.json()

    @login_required
    def get_my_market_listings(self) -> dict:
        response = self._session.get(f'{SteamUrl.COMMUNITY_URL}/market')
        if response.status_code != HTTPStatus.OK:
            raise ApiException(f'There was a problem getting the listings. HTTP code: {response.status_code}')

        assets_descriptions = json.loads(text_between(response.text, 'var g_rgAssets = ', ';\n'))
        listing_id_to_assets_address = get_listing_id_to_assets_address_from_html(response.text)
        listings = get_market_listings_from_html(response.text)
        listings = merge_items_with_descriptions_from_listing(
            listings, listing_id_to_assets_address, assets_descriptions,
        )

        if '<span id="tabContentsMyActiveMarketListings_end">' in response.text:
            n_showing = int(text_between(response.text, '<span id="tabContentsMyActiveMarketListings_end">', '</span>'))
            n_total = int(
                text_between(response.text, '<span id="tabContentsMyActiveMarketListings_total">', '</span>').replace(
                    ',', '',
                ),
            )

            if n_showing < n_total < 1000:
                url = f'{SteamUrl.COMMUNITY_URL}/market/mylistings/render/?query=&start={n_showing}&count={-1}'
                response = self._session.get(url)
                if response.status_code != HTTPStatus.OK:
                    raise ApiException(f'There was a problem getting the listings. HTTP code: {response.status_code}')

                jresp = response.json()
                listing_id_to_assets_address = get_listing_id_to_assets_address_from_html(jresp.get('hovers'))
                listings_2 = get_market_sell_listings_from_api(jresp.get('results_html'))
                listings_2 = merge_items_with_descriptions_from_listing(
                    listings_2, listing_id_to_assets_address, jresp.get('assets'),
                )
                listings['sell_listings'] = {**listings['sell_listings'], **listings_2['sell_listings']}
            else:
                for i in range(0, n_total, 100):
                    url = f'{SteamUrl.COMMUNITY_URL}/market/mylistings/?query=&start={n_showing + i}&count={100}'
                    response = self._session.get(url)
                    if response.status_code != HTTPStatus.OK:
                        raise ApiException(
                            f'There was a problem getting the listings. HTTP code: {response.status_code}',
                        )
                    jresp = response.json()
                    listing_id_to_assets_address = get_listing_id_to_assets_address_from_html(jresp.get('hovers'))
                    listings_2 = get_market_sell_listings_from_api(jresp.get('results_html'))
                    listings_2 = merge_items_with_descriptions_from_listing(
                        listings_2, listing_id_to_assets_address, jresp.get('assets'),
                    )
                    listings['sell_listings'] = {**listings['sell_listings'], **listings_2['sell_listings']}

        return listings

    @login_required
    def create_sell_order(self, assetid: str, game: GameOptions, money_to_receive: str) -> dict:
        data = {
            'assetid': assetid,
            'sessionid': self._session_id,
            'contextid': game.context_id,
            'appid': game.app_id,
            'amount': 1,
            'price': money_to_receive,
        }
        headers = {'Referer': f'{SteamUrl.COMMUNITY_URL}/profiles/{self._steam_guard["steamid"]}/inventory'}

        response = self._session.post(f'{SteamUrl.COMMUNITY_URL}/market/sellitem/', data, headers=headers).json()
        has_pending_confirmation = 'pending confirmation' in response.get('message', '')
        if response.get('needs_mobile_confirmation') or (not response.get('success') and has_pending_confirmation):
            return self._confirm_sell_listing(assetid)

        return response

    @login_required
    def create_buy_order(
        self,
        market_name: str,
        price_single_item: str,
        quantity: int,
        game: GameOptions,
        currency: Currency = Currency.USD,
    ) -> dict:
        data = {
            'sessionid': self._session_id,
            'currency': currency.value,
            'appid': game.app_id,
            'market_hash_name': market_name,
            'price_total': str(Decimal(price_single_item) * Decimal(quantity)),
            'quantity': quantity,
        }
        headers = {
            'Referer': f'{SteamUrl.COMMUNITY_URL}/market/listings/{game.app_id}/{urllib.parse.quote(market_name)}',
        }

        response = self._session.post(f'{SteamUrl.COMMUNITY_URL}/market/createbuyorder/', data, headers=headers).json()

        if (success := response.get('success')) != 1:
            raise ApiException(
                f'There was a problem creating the order. Are you using the right currency? success: {success}',
            )

        return response

    def _confirm_buy_listing(self) -> dict:
        con_executor = ConfirmationExecutor(
            self._steam_guard['identity_secret'], self._steam_guard['steamid'], self._session
        )
        confirmations = con_executor._get_confirmations()
        for confirmation in confirmations:
            con_executor._send_confirmation(confirmation)
        return {'success': True, 'message': 'All pending confirmations have been accepted.'}

    @login_required
    def buy_item(
        self,
        market_name: str,
        market_id: str,
        price: int,
        fee: int,
        game: GameOptions,
        currency: Currency = Currency.USD,
    ) -> dict:
        data = {
            'sessionid': self._session.cookies.get_dict("steamcommunity.com")['sessionid'],
            'currency': currency.value,
            'subtotal': price - fee,
            'fee': fee,
            'total': price,
            'quantity': '1',
            'billing_state': '',
            'save_my_address': '0',
            'confirmation': '0'
        }
        headers = {
            'Referer': f'{SteamUrl.COMMUNITY_URL}/market/listings/{game.app_id}/{urllib.parse.quote(market_name)}',
        }
        response = self._session.post(
            f'{SteamUrl.COMMUNITY_URL}/market/buylisting/{market_id}', data, headers=headers,
        ).json()

        if response.get('wallet_info') and response.get('wallet_info').get('success') == 1:
            return response

        if response.get('success') == 22 and response.get('confirmation', {}).get('confirmation_id'):
            try:
                confirmation_id = response['confirmation']['confirmation_id']
                data['confirmation'] = confirmation_id
                self._confirm_buy_listing()
                time.sleep(1)

                final_response = self._session.post(
                    f'{SteamUrl.COMMUNITY_URL}/market/buylisting/{market_id}', data=data, headers=headers
                ).json()
                return final_response
            except (KeyError, TypeError):
                raise ApiException('Steam requested confirmation, but returned invalid data (confirmation_id).')
            except Exception as e:
                raise ApiException(f'An error occurred during the second confirmation step: {e}')

        message = response.get('message')
        if not message and response.get('wallet_info'):
            message = response['wallet_info'].get('message')
        raise ApiException(f'Failed to buy item. Steam message: {message}')


    @login_required
    def cancel_sell_order(self, sell_listing_id: str) -> None:
        data = {'sessionid': self._session_id}
        headers = {'Referer': f'{SteamUrl.COMMUNITY_URL}/market/'}
        url = f'{SteamUrl.COMMUNITY_URL}/market/removelisting/{sell_listing_id}'

        response = self._session.post(url, data=data, headers=headers)
        if response.status_code != HTTPStatus.OK:
            raise ApiException(f'There was a problem removing the listing. HTTP code: {response.status_code}')

    @login_required
    def cancel_buy_order(self, buy_order_id) -> dict:
        data = {'sessionid': self._session_id, 'buy_orderid': buy_order_id}
        headers = {'Referer': f'{SteamUrl.COMMUNITY_URL}/market'}
        response = self._session.post(f'{SteamUrl.COMMUNITY_URL}/market/cancelbuyorder/', data, headers=headers).json()

        if (success := response.get('success')) != 1:
            raise ApiException(f'There was a problem canceling the order. success: {success}')

        return response

    def _confirm_sell_listing(self, asset_id: str) -> dict:
        con_executor = ConfirmationExecutor(
            self._steam_guard['identity_secret'], self._steam_guard['steamid'], self._session,
        )
        return con_executor.confirm_sell_listing(asset_id)
