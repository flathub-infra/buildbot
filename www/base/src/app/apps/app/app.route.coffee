class State extends Config
    constructor: ($stateProvider) ->

        # Name of the state
        name = 'app'

        # Configuration
        cfg =
            tabid: 'apps'
            pageTitle: _.template("Flathub: app <%= app %>")

        # Register new state
        state =
            controller: "#{name}Controller"
            templateUrl: "views/#{name}.html"
            name: name
            url: '/apps/:app?numbuilds'
            data: cfg

        $stateProvider.state(state)
