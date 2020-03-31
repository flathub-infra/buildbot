class AppState {
    constructor($stateProvider) {

        // Name of the state
        const name = 'app';

        // Configuration
        const cfg = {
            tabid: 'apps',
            pageTitle: _.template("Flathub: app <%= app %>")
        };

        // Register new state
        const state = {
            controller: `${name}Controller`,
            template: require('./app.tpl.jade'),
            name,
            url: '/apps/:app?numbuilds',
            data: cfg
        };

        $stateProvider.state(state);
    }
}


angular.module('app')
.config(['$stateProvider', AppState]);
