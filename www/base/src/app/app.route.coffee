class Route extends Config
    constructor: ($urlRouterProvider, glMenuServiceProvider, config) ->
        $urlRouterProvider.otherwise('/')
        # the app title needs to be < 18 chars else the UI looks bad
        # we try to find best option
        apptitle = "Flathub builds"
        glMenuServiceProvider.setAppTitle(apptitle)
        # all states config are in the modules
