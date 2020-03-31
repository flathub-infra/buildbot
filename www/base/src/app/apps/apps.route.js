class AppsState {
    constructor($stateProvider, glMenuServiceProvider, bbSettingsServiceProvider) {

        // Name of the state
        const name = 'apps';

        // Menu configuration
        glMenuServiceProvider.addGroup({
            name: "apps",
            caption: 'Apps',
            icon: 'cubes',
            order: 5
        });

        // Configuration
        const cfg = {
            group: "apps",
            caption: 'Apps'
        };

        // Register new state
        const state = {
            controller: `${name}Controller`,
            template: require('./apps.tpl.jade'),
            name,
            url: '/apps',
            data: cfg,
            reloadOnSearch: false
        };

        $stateProvider.state(state);
    }
}


angular.module('app')
.config(['$stateProvider', 'glMenuServiceProvider', 'bbSettingsServiceProvider', AppsState]);
