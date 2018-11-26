class State extends Config
    constructor: ($stateProvider, glMenuServiceProvider, bbSettingsServiceProvider) ->

        # Name of the state
        name = 'apps'

        # Menu configuration
        glMenuServiceProvider.addGroup
            name: "apps"
            caption: 'Apps'
            icon: 'cubes'
            order: 5

        # Configuration
        cfg =
            group: "apps"
            caption: 'Apps'

        # Register new state
        state =
            controller: "#{name}Controller"
            templateUrl: "views/#{name}.html"
            name: name
            url: '/apps'
            data: cfg
            reloadOnSearch: false

        $stateProvider.state(state)
