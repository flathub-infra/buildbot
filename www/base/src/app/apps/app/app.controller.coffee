class App extends Controller
    constructor: ($rootScope, $scope, dataService, $stateParams, resultsService,
        glBreadcrumbService, $state, glTopbarContextualActionsService, $q, $window) ->
        # make resultsService utilities available in the template
        _.mixin($scope, resultsService)
        appname = $stateParams.app
        $scope.appname=appname
        data = dataService.open().closeOnDestroy($scope)
        data.getBuilders('Builds').onNew = (builder) ->
            $scope.builder = builder
            breadcrumb = [
                    caption: "Apps"
                    sref: "apps"
                ,
                    caption: appname
                    sref: "app({app:'" + appname + "'})"
            ]
            glBreadcrumbService.setBreadcrumb(breadcrumb)
            refreshContextMenu = ->
                if $scope.$$destroyed
                    return
                actions = []
                _.forEach $scope.forceschedulers, (sch) ->
                    actions.push
                        caption: sch.button_name
                        extra_class: "btn-primary"
                        action: ->
                            $state.go("builder.forcebuilder",
                                scheduler:sch.name, builder:$scope.builder.builderid, buildname:$scope.appname)
                glTopbarContextualActionsService.setContextualActions(actions)

             builder.getForceschedulers().onChange = (forceschedulers) ->
                $scope.forceschedulers = forceschedulers
                refreshContextMenu()
                # reinstall contextual actions when coming back from forcesched
                $scope.$on '$stateChangeSuccess', ->
                    refreshContextMenu()

            $scope.numbuilds = 100
            if $stateParams.numbuilds?
                $scope.numbuilds = +$stateParams.numbuilds
            $scope.runningBuilds = builder.getBuilds
                flathub_name__eq: appname
                complete: false
                property: ["owners"]
                order: '-number'
            $scope.unpublishedBuilds = builder.getBuilds
                flathub_name__eq: appname
                complete: true
                flathub_repo_status__le: 1
                property: ["owners"]
                order: '-number'
            $scope.recentBuilds = builder.getBuilds
                complete: true
                flathub_name__eq: appname
                flathub_repo_status__gt: 1
                property: ["owners"]
                limit: $scope.numbuilds
                order: '-number'

            $scope.changeCount = 0
            refreshGraph = ->
                $scope.changeCount = $scope.changeCount + 1
                if $scope.changeCount == 1 # We wait until both unpublished and recent are fetched
                        return
                $scope.successful_builds = []
                addBuild = (b) ->
                    if b.results == 0 and b.complete_at != null
                        b.duration = b.complete_at - b.started_at
                        $scope.successful_builds.push(b)
                $scope.unpublishedBuilds.forEach addBuild
                $scope.recentBuilds.forEach addBuild

            $scope.recentBuilds.onChange= ->
                refreshGraph()
            $scope.unpublishedBuilds.onChange= ->
                refreshGraph()
